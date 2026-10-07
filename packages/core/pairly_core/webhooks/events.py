# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhook event types and the transactional outbox.

Event types are declared, never invented at runtime: core declares ``CORE_EVENTS``; a
workflow plugin may add its own through an optional ``webhook_events`` attribute (a
sequence of ``EventType`` whose keys start with the plugin id, for example
``pay.invoice.approved``). ``build_event_registry`` collects them; the API and the
worker install it at startup.

``publish_event`` inserts one ``webhook_outbox`` row in the caller's transaction, so the
event exists if and only if the business change commits (a rolled-back transaction
emits nothing). The worker later turns outbox rows into deliveries
(``pairly_core.webhooks.delivery``).

Envelope sent to endpoints (``envelope``)::

    {"id": "evt_<32 hex>", "type": "approval.decided", "created_at": "<ISO 8601 UTC>",
     "org_id": "<uuid>", "data": {...}}
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.db import TenantError, current_org
from pairly_core.webhooks.models import WebhookOutbox

EVENT_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,4}$")
MAX_DATA_BYTES: Final = 64 * 1024

APPROVAL_DECIDED: Final = "approval.decided"
DOCUMENT_RECEIVED: Final = "document.received"
DECISION_RECORDED: Final = "decision.recorded"


class EventRegistryError(ValueError):
    """An invalid or unknown event type (a programming error)."""


@dataclass(frozen=True, slots=True)
class EventType:
    key: str
    description: str

    def __post_init__(self) -> None:
        if not EVENT_TYPE_RE.fullmatch(self.key) or len(self.key) > 100:
            raise EventRegistryError(f"invalid event type {self.key!r}")


CORE_EVENTS: tuple[EventType, ...] = (
    EventType(APPROVAL_DECIDED, "A person decided an approval step"),
    EventType(DOCUMENT_RECEIVED, "A new document was received (upload, email or API)"),
    EventType(DECISION_RECORDED, "A decision record was written"),
)


class EventRegistry:
    def __init__(self, events: Iterable[EventType] = CORE_EVENTS) -> None:
        self._events: dict[str, EventType] = {}
        for event in events:
            self.register(event)

    def register(self, event: EventType) -> None:
        if event.key in self._events:
            raise EventRegistryError(f"duplicate event type {event.key!r}")
        self._events[event.key] = event

    def __contains__(self, key: object) -> bool:
        return key in self._events

    @property
    def events(self) -> list[EventType]:
        return sorted(self._events.values(), key=lambda e: e.key)

    def require(self, key: str) -> EventType:
        try:
            return self._events[key]
        except KeyError:
            raise EventRegistryError(f"unknown event type {key!r}") from None


def build_event_registry(plugins: Iterable[Any] = ()) -> EventRegistry:
    """Core events plus every plugin's optional ``webhook_events``."""
    registry = EventRegistry()
    for plugin in plugins:
        for event in getattr(plugin, "webhook_events", ()) or ():
            if not isinstance(event, EventType):
                raise EventRegistryError(f"plugin {plugin.id!r}: webhook_events must be EventType")
            if not event.key.startswith(f"{plugin.id}."):
                raise EventRegistryError(
                    f"plugin {plugin.id!r}: event {event.key!r} is outside its namespace"
                )
            registry.register(event)
    return registry


_registry = EventRegistry()


def install_event_registry(registry: EventRegistry) -> None:
    global _registry
    _registry = registry


def event_registry() -> EventRegistry:
    return _registry


def _check_data(data: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = json.dumps(dict(data), allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise EventRegistryError("event data must be plain JSON") from None
    if len(raw.encode("utf-8")) > MAX_DATA_BYTES:
        raise EventRegistryError("event data is too large")
    clean: dict[str, Any] = json.loads(raw)
    return clean


async def publish_event(
    session: AsyncSession, event_type: str, data: Mapping[str, Any]
) -> uuid.UUID:
    """Write ``event_type`` to the outbox in the session's transaction. Returns its id."""
    org = current_org(session)
    if org is None:
        raise TenantError("webhook events need a tenant context")
    _registry.require(event_type)
    event_id = uuid.uuid4()
    await session.execute(
        insert(WebhookOutbox).values(
            id=event_id, org_id=org, event_type=event_type, data=_check_data(data)
        )
    )
    return event_id


def event_ref(event_id: uuid.UUID) -> str:
    return f"evt_{event_id.hex}"


def envelope(
    *, event_id: uuid.UUID, event_type: str, created_at: datetime, org_id: uuid.UUID, data: Any
) -> dict[str, Any]:
    return {
        "id": event_ref(event_id),
        "type": event_type,
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "org_id": str(org_id),
        "data": data,
    }


def serialise(payload: Mapping[str, Any]) -> bytes:
    """The exact body bytes sent (and signed): canonical JSON."""
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

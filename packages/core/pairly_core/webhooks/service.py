# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhook endpoint management (the API side).

* Only a person with ``core.webhooks.manage`` (step-up) creates, changes or rotates.
* The URL passes the SSRF checks (``ssrf``) when it is saved, DNS included. Delivery
  checks it again every time.
* Event types must be registered (``events.event_registry``).
* The signing secret (``whsec_...``) is returned once, at creation or rotation, and is
  stored encrypted. Audit events never contain it, nor the URL's path or query (they
  may hold tokens): only the host.
* Re-enabling a disabled endpoint resets its failure count.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, NotFoundError
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError
from pairly_core.webhooks.events import event_registry
from pairly_core.webhooks.models import WebhookDelivery, WebhookEndpoint
from pairly_core.webhooks.settings import WebhookSettings
from pairly_core.webhooks.ssrf import Resolver, parse_target, resolve_target

SECRET_PREFIX: Final = "whsec_"  # noqa: S105  # nosec B105 (a prefix, not a secret)
MAX_EVENT_TYPES: Final = 100


class WebhookAction:
    CREATED: Final = "core.webhook.endpoint_created"
    UPDATED: Final = "core.webhook.endpoint_updated"
    SECRET_ROTATED: Final = "core.webhook.secret_rotated"  # noqa: S105  # nosec B105 (an action name)


@dataclass(frozen=True, slots=True)
class EndpointWithSecret:
    endpoint: WebhookEndpoint
    secret: str  # returned once; never logged


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def _require_person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise PermissionDeniedError("only a person can manage webhook endpoints")


def check_event_types(event_types: Sequence[str]) -> list[str]:
    clean = sorted(set(event_types))
    if not clean or len(clean) > MAX_EVENT_TYPES:
        raise BadRequestError(f"choose 1 to {MAX_EVENT_TYPES} event types")
    registry = event_registry()
    unknown = [e for e in clean if e not in registry]
    if unknown:
        raise BadRequestError(f"unknown event types: {', '.join(unknown[:5])}")
    return clean


async def check_url(url: str, settings: WebhookSettings, resolver: Resolver) -> str:
    target = parse_target(url, allow_http=settings.webhook_allow_http)
    await resolve_target(target, resolver, allow_private=settings.webhook_allow_private_targets)
    return url


async def create_endpoint(
    session: AsyncSession,
    actor: Actor,
    *,
    url: str,
    event_types: Sequence[str],
    description: str | None,
    settings: WebhookSettings,
    resolver: Resolver,
) -> EndpointWithSecret:
    _require_person(actor)
    clean_types = check_event_types(event_types)
    await check_url(url, settings, resolver)
    secret = new_secret()
    endpoint_id = uuid.uuid4()
    endpoint = WebhookEndpoint(
        id=endpoint_id,
        org_id=actor.org_id,
        url=url,
        description=(description or None),
        secret=seal(WebhookEndpoint, "secret", secret, org_id=actor.org_id, row_id=endpoint_id),
        event_types=clean_types,
        active=True,
        consecutive_failures=0,
        created_by=actor.id,
    )
    session.add(endpoint)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        WebhookAction.CREATED,
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        payload={"host": parse_target(url, allow_http=True).host, "event_types": clean_types},
    )
    return EndpointWithSecret(endpoint, secret)


async def get_endpoint(
    session: AsyncSession, endpoint_id: uuid.UUID, *, lock: bool = False
) -> WebhookEndpoint:
    query = select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise NotFoundError("webhook endpoint not found")
    return row


async def list_endpoints(session: AsyncSession) -> list[WebhookEndpoint]:
    return list(await session.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at)))


async def update_endpoint(
    session: AsyncSession,
    actor: Actor,
    endpoint_id: uuid.UUID,
    *,
    url: str | None = None,
    event_types: Sequence[str] | None = None,
    description: str | None = None,
    active: bool | None = None,
    settings: WebhookSettings,
    resolver: Resolver,
) -> WebhookEndpoint:
    _require_person(actor)
    endpoint = await get_endpoint(session, endpoint_id, lock=True)
    changed: dict[str, object] = {}
    if url is not None and url != endpoint.url:
        endpoint.url = await check_url(url, settings, resolver)
        changed["host"] = parse_target(url, allow_http=True).host
    if event_types is not None:
        clean = check_event_types(event_types)
        if clean != list(endpoint.event_types):
            endpoint.event_types = clean
            changed["event_types"] = clean
    if description is not None and description != (endpoint.description or ""):
        endpoint.description = description or None
        changed["description"] = True
    if active is not None and active != endpoint.active:
        now = datetime.now(UTC)
        endpoint.active = active
        if active:
            # A fresh start: the failure count from before the outage no longer applies.
            await check_url(endpoint.url, settings, resolver)
            endpoint.consecutive_failures = 0
            endpoint.disabled_at = None
            endpoint.disabled_reason = None
        else:
            endpoint.disabled_at = now
            endpoint.disabled_reason = "disabled_by_admin"
        changed["active"] = active
    if not changed:
        return endpoint
    endpoint.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        WebhookAction.UPDATED,
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        payload=changed,
    )
    return endpoint


async def rotate_secret(
    session: AsyncSession, actor: Actor, endpoint_id: uuid.UUID
) -> EndpointWithSecret:
    _require_person(actor)
    endpoint = await get_endpoint(session, endpoint_id, lock=True)
    secret = new_secret()
    endpoint.secret = seal(  # type: ignore[assignment]
        WebhookEndpoint, "secret", secret, org_id=endpoint.org_id, row_id=endpoint.id
    )
    endpoint.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        WebhookAction.SECRET_ROTATED,
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
    )
    return EndpointWithSecret(endpoint, secret)


async def list_deliveries(
    session: AsyncSession, endpoint_id: uuid.UUID, *, limit: int = 100
) -> list[WebhookDelivery]:
    await get_endpoint(session, endpoint_id)
    return list(
        await session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint_id)
            .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
            .limit(max(1, min(limit, 500)))
        )
    )

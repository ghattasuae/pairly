# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Proofly Technology
"""Webhook helpers: verify a delivery, parse it, and build signed test events.

Every delivery carries ``Pairly-Signature: t=<unix seconds>,v1=<hex>`` where ``<hex>`` is
``HMAC-SHA256(secret, "<t>." + body)`` over the exact body bytes, keyed with the
endpoint's signing secret (``whsec_...``). This module mirrors the engine
(``pairly_core.webhooks.signature``, ``pairly_core.webhooks.events``) with the standard
library only; ``tests/test_webhooks.py`` checks both sign the same bytes the same way.

    from pairly.webhooks import construct_event

    event = construct_event(request_body, request.headers.get("Pairly-Signature"), secret)
    if event.type == "batch.completed":
        ...

Always verify before parsing, with the raw body (not re-serialised JSON).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, TypedDict, cast

SIGNATURE_HEADER: Final = "Pairly-Signature"
EVENT_ID_HEADER: Final = "Pairly-Event-Id"
EVENT_TYPE_HEADER: Final = "Pairly-Event-Type"
SCHEME: Final = "v1"
DEFAULT_TOLERANCE_SECONDS: Final = 300
MAX_HEADER_LENGTH: Final = 4096

EventType = Literal["approval.decided", "document.received", "decision.recorded", "batch.completed"]


class WebhookSignatureError(ValueError):
    """The signature header is missing, malformed, too old, or does not match."""


def _as_bytes(payload: bytes | str) -> bytes:
    return payload.encode("utf-8") if isinstance(payload, str) else payload


def compute_signature(secret: str, timestamp: int, payload: bytes | str) -> str:
    """Hex ``HMAC-SHA256(secret, "<timestamp>." + payload)``."""
    signed = str(int(timestamp)).encode("ascii") + b"." + _as_bytes(payload)
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_payload(secret: str, payload: bytes | str, timestamp: int | None = None) -> str:
    """The ``Pairly-Signature`` header value for ``payload`` (for tests and tools)."""
    t = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={t},{SCHEME}={compute_signature(secret, t, payload)}"


def verify_signature(
    payload: bytes | str,
    header: str | None,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    *,
    now: int | None = None,
) -> int:
    """Check a ``Pairly-Signature`` header against the raw body. Returns its timestamp.

    Raises ``WebhookSignatureError`` when the header is missing or malformed, the
    timestamp is more than ``tolerance`` seconds away from now (either way: replay
    protection), or no ``v1`` value matches (several are allowed during a secret
    rotation). The comparison is constant time.
    """
    if not secret:
        raise WebhookSignatureError("no signing secret configured")
    if not header or len(header) > MAX_HEADER_LENGTH:
        raise WebhookSignatureError("missing or oversized signature header")
    timestamp: int | None = None
    candidates: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            raise WebhookSignatureError("malformed signature header")
        if key == "t":
            if timestamp is not None or not value.isdigit() or len(value) > 12:
                raise WebhookSignatureError("malformed signature timestamp")
            timestamp = int(value)
        elif key == SCHEME:
            candidates.append(value)
    if timestamp is None or not candidates:
        raise WebhookSignatureError("the signature header needs t and v1")
    current = int(time.time()) if now is None else int(now)
    if tolerance < 0 or abs(current - timestamp) > tolerance:
        raise WebhookSignatureError("the signature timestamp is outside the tolerance")
    expected = compute_signature(secret, timestamp, payload)
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise WebhookSignatureError("no signature matches")
    return timestamp


# The engine's name for the same function.
verify_webhook_signature = verify_signature


# ------------------------------------------------------------------ typed events


class ApprovalDecidedData(TypedDict):
    approval_id: str
    run_id: str
    workflow: str
    approval_type: str
    step: str
    decision_id: str
    decision: Literal["approve", "reject", "request_changes"]
    approval_status: str
    entity_type: str | None
    entity_id: str | None


class DocumentReceivedData(TypedDict):
    document_id: str
    workflow: str
    source: str
    media_type: str
    size_bytes: int
    sha256: str


class DecisionRecordedData(TypedDict):
    decision_id: str
    kind: str
    workflow: str | None
    entity_type: str | None
    entity_id: str | None
    final_action: str | None
    supersedes: str | None
    content_hash: str
    decided_at: str


class BatchItemData(TypedDict):
    id: str
    position: int
    source: str
    status: str
    reason: str | None
    document_id: str | None
    entity_type: str | None
    entity_id: str | None


class BatchCompletedData(TypedDict):
    batch_id: str
    workflow: str
    status: str
    metadata: dict[str, Any]
    counts: dict[str, int]
    items: list[BatchItemData]


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One delivery: ``{"id", "type", "created_at", "org_id", "data"}``."""

    id: str
    type: str
    created_at: str
    org_id: str
    data: dict[str, Any]

    def approval_decided(self) -> ApprovalDecidedData:
        return cast(ApprovalDecidedData, self._data("approval.decided"))

    def document_received(self) -> DocumentReceivedData:
        return cast(DocumentReceivedData, self._data("document.received"))

    def decision_recorded(self) -> DecisionRecordedData:
        return cast(DecisionRecordedData, self._data("decision.recorded"))

    def batch_completed(self) -> BatchCompletedData:
        return cast(BatchCompletedData, self._data("batch.completed"))

    def _data(self, expected: str) -> dict[str, Any]:
        if self.type != expected:
            raise ValueError(f"this is a {self.type} event, not {expected}")
        return self.data

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "created_at": self.created_at,
            "org_id": self.org_id,
            "data": self.data,
        }


def parse_event(payload: bytes | str) -> WebhookEvent:
    """Parse a (verified) body. Raises ``ValueError`` when it is not an event."""
    try:
        raw = json.loads(_as_bytes(payload))
    except ValueError:
        raise ValueError("the webhook body is not JSON") from None
    if not isinstance(raw, dict):
        raise ValueError("the webhook body is not an event")
    try:
        event = WebhookEvent(
            id=str(raw["id"]),
            type=str(raw["type"]),
            created_at=str(raw["created_at"]),
            org_id=str(raw["org_id"]),
            data=dict(raw["data"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("the webhook body is not an event") from None
    return event


def construct_event(
    payload: bytes | str,
    header: str | None,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> WebhookEvent:
    """Verify the signature, then parse: the one call a webhook handler needs."""
    verify_signature(payload, header, secret, tolerance)
    return parse_event(payload)


# ------------------------------------------------------------------ building events


def serialise(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes Pairly sends (and signs): canonical JSON, like the engine."""
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def build_event(
    event_type: str,
    data: Mapping[str, Any],
    *,
    org_id: str | uuid.UUID,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> WebhookEvent:
    """An event shaped exactly like a Pairly delivery (for tests and local tools)."""
    when = (created_at or datetime.now(UTC)).astimezone(UTC)
    return WebhookEvent(
        id=event_id or f"evt_{uuid.uuid4().hex}",
        type=event_type,
        created_at=when.isoformat().replace("+00:00", "Z"),
        org_id=str(org_id),
        data=dict(data),
    )


def approval_decided_event(data: ApprovalDecidedData, *, org_id: str | uuid.UUID) -> WebhookEvent:
    return build_event("approval.decided", data, org_id=org_id)


def document_received_event(data: DocumentReceivedData, *, org_id: str | uuid.UUID) -> WebhookEvent:
    return build_event("document.received", data, org_id=org_id)


def decision_recorded_event(data: DecisionRecordedData, *, org_id: str | uuid.UUID) -> WebhookEvent:
    return build_event("decision.recorded", data, org_id=org_id)


def batch_completed_event(data: BatchCompletedData, *, org_id: str | uuid.UUID) -> WebhookEvent:
    return build_event("batch.completed", data, org_id=org_id)


def signed_delivery(
    event: WebhookEvent, secret: str, *, timestamp: int | None = None
) -> tuple[bytes, dict[str, str]]:
    """The body and headers Pairly would send for ``event`` (to test your handler)."""
    body = serialise(event.to_dict())
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(secret, body, timestamp),
        EVENT_ID_HEADER: event.id,
        EVENT_TYPE_HEADER: event.type,
    }
    return body, headers

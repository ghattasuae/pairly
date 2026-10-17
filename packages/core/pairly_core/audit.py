# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hash-chained, append-only audit log.

Every organisation has its own chain in ``audit_events``. Event ``seq`` numbers start at 1
and grow by exactly 1. Each event stores the hash of the previous event of the same
organisation (``prev_hash``) and its own hash:

    hash = sha256( bytes.fromhex(prev_hash) || canonical_event_json(event) ).hexdigest()

The first event of an organisation uses ``GENESIS_HASH`` (64 zeros) as ``prev_hash``.
``canonical_event_json`` is the single definition of the hashed bytes; see its docstring.
Changing any hashed field, removing an event from the middle, or reordering events
breaks the chain, and ``verify_chain`` reports the first broken ``seq``.

Writes go through ``append_audit`` only. It takes a per-organisation transaction-level
advisory lock, reads the last event of the chain in the same transaction and inserts the
next one, so concurrent appends from many sessions or processes still form one chain.
The event commits (or rolls back) together with the change it records.

Database guarantees (migration 0002): the application role has INSERT and SELECT only,
triggers reject UPDATE, DELETE and TRUNCATE for every role (owner included), an insert
trigger rejects any row that does not link to the current end of its chain, and Row
Level Security keeps each organisation's events private.

Tail truncation (removing the newest events, by someone who can disable the triggers)
leaves a shorter chain that still links. ``pairly_core.audit_anchor`` closes that gap:
a nightly job signs each organisation's head (seq, hash) with an Ed25519 key that is
not in the database and writes it to write-once object storage. ``verify_chain`` and
``verify_records`` take those anchored heads and fail when the chain no longer holds
one of them (``anchor_mismatch``) or ends before one (``anchor_truncated``).

Payloads must not hold secrets and should hold as little personal data as possible:
ids and short facts, never document contents, tokens or passwords. ``append_audit``
rejects floats, NUL characters, oversized payloads and keys that look like secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Select, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.sql import ColumnElement

from pairly_core.db import TenantError, TenantSession, current_org
from pairly_core.models.audit import AuditEvent
from pairly_core.rbac import Actor, ActorKind, Denial, PermissionDeniedError

GENESIS_HASH: Final = "0" * 64
ADVISORY_LOCK_NAMESPACE: Final = "pairly.audit:"

# session.info keys. The counters are per transaction (reset when the root transaction
# ends) and drive the request commit hook in ``pairly_core.identity``.
INFO_REQUEST_ID: Final = "pairly.request_id"
_INFO_EVENTS = "pairly.audit.events"
_INFO_WRITES = "pairly.audit.orm_writes"

MAX_PAYLOAD_BYTES: Final = 16 * 1024
MAX_PAYLOAD_DEPTH: Final = 8
MAX_STRING_LENGTH: Final = 2000
_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,5}$")
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_WORKFLOW_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Payload keys that suggest a secret. Matched against the lower-cased key.
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|credential|private_?key|api_?key|authorization|cookie|"
    r"session_?id|(^|[_.-])(token|access_token|refresh_token|id_token|jwt|otp|totp|pin|"
    r"cvv|iban|card_number)$)"
)


class AuditError(ValueError):
    """An audit event is malformed (a programming error, never user input)."""


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    API_KEY = "api_key"
    # An external person through a workflow portal (``ActorKind.CANDIDATE``).
    CANDIDATE = "candidate"
    # A vendor's contact through a workflow's vendor portal (``ActorKind.VENDOR``).
    VENDOR = "vendor"


class AuditAction:
    """Action names used by core. Workflows use ``<workflow>.<entity>.<verb>``."""

    AUTHZ_DENIED: Final = "core.authz.denied"
    # Denials over an actor's rate limit, summarised (``pairly_core.audit_anchor.denials``).
    AUTHZ_DENIALS_SUPPRESSED: Final = "core.authz.denials_suppressed"
    # Safety net: a request changed rows through the ORM but recorded no event itself.
    REQUEST_WRITE: Final = "core.request.write"
    MEMBERSHIP_GRANTED: Final = "core.membership.granted"
    MEMBERSHIP_REVOKED: Final = "core.membership.revoked"
    MEMBERSHIP_CHANGED: Final = "core.membership.changed"
    ORG_CREATED: Final = "core.org.created"
    INVITATION_CREATED: Final = "core.invitation.created"
    INVITATION_REVOKED: Final = "core.invitation.revoked"
    INVITATION_ACCEPTED: Final = "core.invitation.accepted"
    ROLE_CREATED: Final = "core.role.created"
    ROLE_CHANGED: Final = "core.role.changed"
    ROLE_DELETED: Final = "core.role.deleted"
    AUDIT_EXPORTED: Final = "core.audit.exported"
    AUDIT_CHAIN_BROKEN: Final = "core.audit.chain_verification_failed"


# ------------------------------------------------------------------ canonical form


def format_timestamp(value: datetime) -> str:
    """UTC ISO 8601 with microseconds and a ``Z`` suffix: ``2026-10-07T08:30:00.000000Z``."""
    if value.tzinfo is None:
        raise AuditError("timestamps must be timezone aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One audit event, as stored."""

    id: uuid.UUID
    org_id: uuid.UUID
    seq: int
    occurred_at: datetime
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    workflow: str | None
    payload: Mapping[str, Any]
    request_id: str | None
    prev_hash: str
    hash: str

    def hashed_fields(self) -> dict[str, Any]:
        """The fields covered by ``hash``, in their canonical value form."""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "seq": self.seq,
            "occurred_at": format_timestamp(self.occurred_at),
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "workflow": self.workflow,
            "payload": dict(self.payload),
            "request_id": self.request_id,
        }

    def to_json(self) -> dict[str, Any]:
        """JSON-ready form used by the API and the exports (hashed fields plus hashes)."""
        return self.hashed_fields() | {"prev_hash": self.prev_hash, "hash": self.hash}

    @classmethod
    def from_row(cls, row: Any) -> AuditRecord:
        return cls(
            id=row.id,
            org_id=row.org_id,
            seq=row.seq,
            occurred_at=row.occurred_at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            workflow=row.workflow,
            payload=row.payload,
            request_id=row.request_id,
            prev_hash=row.prev_hash,
            hash=row.hash,
        )

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> AuditRecord:
        """Parse one event of a JSON export (for offline verification)."""
        stamp = str(data["occurred_at"])
        return cls(
            id=uuid.UUID(str(data["id"])),
            org_id=uuid.UUID(str(data["org_id"])),
            seq=int(data["seq"]),
            occurred_at=datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
            actor_type=str(data["actor_type"]),
            actor_id=data.get("actor_id"),
            action=str(data["action"]),
            entity_type=data.get("entity_type"),
            entity_id=data.get("entity_id"),
            workflow=data.get("workflow"),
            payload=dict(data.get("payload") or {}),
            request_id=data.get("request_id"),
            prev_hash=str(data["prev_hash"]),
            hash=str(data["hash"]),
        )


def canonical_json(value: Any) -> bytes:
    """Canonical JSON bytes: UTF-8, keys sorted, no whitespace, no NaN or Infinity.

    Equivalent to ``json.dumps(value, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False, allow_nan=False).encode("utf-8")``. Non-ASCII characters are kept
    as UTF-8 (not ``\\u`` escaped).
    """
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AuditError(f"value is not canonical JSON: {type(exc).__name__}") from None


def canonical_event_json(fields: Mapping[str, Any]) -> bytes:
    """The hashed bytes of one event. THE definition of the audit hash input.

    ``fields`` is ``AuditRecord.hashed_fields()``: exactly these twelve keys, every one
    present (``null`` when empty):

    ``id`` (lowercase hyphenated UUID), ``org_id`` (same), ``seq`` (integer),
    ``occurred_at`` (``format_timestamp``: UTC, microseconds, ``Z``), ``actor_type``,
    ``actor_id``, ``action``, ``entity_type``, ``entity_id``, ``workflow``, ``payload``
    (JSON object) and ``request_id``.

    They are serialised with ``canonical_json``. ``prev_hash`` and ``hash`` are not part
    of this object: ``prev_hash`` is prepended as raw bytes by ``compute_hash``.
    """
    expected = {
        "id",
        "org_id",
        "seq",
        "occurred_at",
        "actor_type",
        "actor_id",
        "action",
        "entity_type",
        "entity_id",
        "workflow",
        "payload",
        "request_id",
    }
    if set(fields) != expected:
        raise AuditError("hashed fields must be exactly the documented set")
    return canonical_json(dict(fields))


def compute_hash(prev_hash: str, fields: Mapping[str, Any]) -> str:
    """``sha256(bytes.fromhex(prev_hash) + canonical_event_json(fields))`` as lowercase hex."""
    if not _HASH_RE.fullmatch(prev_hash):
        raise AuditError("prev_hash must be 64 lowercase hex characters")
    digest = hashlib.sha256(bytes.fromhex(prev_hash))
    digest.update(canonical_event_json(fields))
    return digest.hexdigest()


# ------------------------------------------------------------------ validation


def _check_ref(name: str, value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        value = str(value)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AuditError(f"invalid {name}")
    return value


def _check_payload_value(value: object, depth: int, path: str) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise AuditError(f"payload nested too deep at {path}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not _INT_MIN <= value <= _INT_MAX:
            raise AuditError(f"payload integer out of range at {path}")
        return
    if isinstance(value, float):
        # jsonb does not round-trip every float exactly; use an int (minor units) or a string.
        raise AuditError(f"payload floats are not allowed (at {path})")
    if isinstance(value, str):
        if "\x00" in value:
            raise AuditError(f"payload strings cannot contain NUL (at {path})")
        if len(value) > MAX_STRING_LENGTH:
            raise AuditError(f"payload string too long at {path}")
        return
    if isinstance(value, list | tuple):
        for i, item in enumerate(value):
            _check_payload_value(item, depth + 1, f"{path}[{i}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not _PAYLOAD_KEY_RE.fullmatch(key):
                raise AuditError(f"invalid payload key at {path}")
            if _SECRET_KEY_RE.search(key.lower()):
                raise AuditError(f"payload key {key!r} looks like a secret; never audit secrets")
            _check_payload_value(item, depth + 1, f"{path}.{key}")
        return
    raise AuditError(f"unsupported payload type {type(value).__name__} at {path}")


def validate_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a plain-JSON copy of ``payload`` or raise ``AuditError``."""
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise AuditError("payload must be a JSON object")
    _check_payload_value(payload, 0, "payload")
    # Round trip through canonical JSON: tuples become lists, the size is checked.
    raw = canonical_json(payload)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise AuditError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
    result: dict[str, Any] = json.loads(raw)
    return result


# ------------------------------------------------------------------ appending


_ACTOR_TYPES = {
    ActorKind.USER: ActorType.USER,
    ActorKind.AGENT: ActorType.AGENT,
    ActorKind.API_KEY: ActorType.API_KEY,
    ActorKind.CANDIDATE: ActorType.CANDIDATE,
    ActorKind.VENDOR: ActorType.VENDOR,
}


def actor_ref(actor: Actor) -> tuple[ActorType, str]:
    return _ACTOR_TYPES[actor.kind], str(actor.id)


def _lock_key(org_id: uuid.UUID) -> str:
    return f"{ADVISORY_LOCK_NAMESPACE}{org_id}"


async def append_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_type: ActorType | str,
    actor_id: str | uuid.UUID | None,
    entity_type: str | None = None,
    entity_id: str | uuid.UUID | None = None,
    workflow: str | None = None,
    payload: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    org_id: uuid.UUID | None = None,
) -> AuditRecord:
    """Append one event to the current organisation's chain, in the session's transaction.

    The session must carry a tenant context (``set_tenant``); ``org_id``, when given, must
    match it. ``request_id`` defaults to the one the request hook stored on the session.
    Must run under READ COMMITTED (the default): the last event is read after the lock is
    taken, so it sees every committed predecessor. A stale read can only fail (unique
    ``(org_id, seq)`` and the link trigger), never fork the chain.
    """
    org = current_org(session)
    if org is None:
        raise TenantError("audit events need a tenant context")
    if org_id is not None and org_id != org:
        raise TenantError("cannot write an audit event for another organisation")
    if not isinstance(action, str) or len(action) > 128 or not _ACTION_RE.fullmatch(action):
        raise AuditError("invalid action")
    try:
        kind = ActorType(actor_type)
    except ValueError:
        raise AuditError("invalid actor_type") from None
    if request_id is None:
        stored = session.info.get(INFO_REQUEST_ID)
        request_id = stored if isinstance(stored, str) else None
    fields: dict[str, Any] = {
        "actor_id": _check_ref("actor_id", actor_id, _REF_RE),
        "entity_type": _check_ref("entity_type", entity_type, _ENTITY_TYPE_RE),
        "entity_id": _check_ref("entity_id", entity_id, _REF_RE),
        "workflow": _check_ref("workflow", workflow, _WORKFLOW_RE),
        "request_id": _check_ref("request_id", request_id, _REQUEST_ID_RE),
    }
    clean_payload = validate_payload(payload)

    # Serialise appends per organisation until this transaction ends.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": _lock_key(org)}
    )
    # The chain head through ``audit_chain_head()`` (migration 0022): it reads past the
    # portal guard, so an event a portal person causes continues the chain too.
    last = (await session.execute(text("SELECT seq, hash FROM audit_chain_head()"))).first()
    if last is not None and last.seq is None:
        last = None
    seq = 1 if last is None else int(last.seq) + 1
    prev_hash = GENESIS_HASH if last is None else str(last.hash)

    record = AuditRecord(
        id=uuid.uuid4(),
        org_id=org,
        seq=seq,
        # Truncated to whole microseconds already; Postgres keeps exactly this value.
        occurred_at=datetime.now(UTC),
        actor_type=kind.value,
        action=action,
        payload=clean_payload,
        prev_hash=prev_hash,
        hash="",
        **fields,
    )
    record = replace(record, hash=compute_hash(prev_hash, record.hashed_fields()))
    values: dict[str, Any] = {
        "id": record.id,
        "org_id": record.org_id,
        "seq": record.seq,
        "occurred_at": record.occurred_at,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "action": record.action,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "workflow": record.workflow,
        "payload": clean_payload,
        "request_id": record.request_id,
        "prev_hash": record.prev_hash,
        "hash": record.hash,
    }
    await session.execute(insert(AuditEvent).values(**values))
    session.info[_INFO_EVENTS] = int(session.info.get(_INFO_EVENTS, 0)) + 1
    return record


async def audit_actor_action(
    session: AsyncSession,
    actor: Actor,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: str | uuid.UUID | None = None,
    workflow: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> AuditRecord:
    """``append_audit`` for an authenticated actor (user or agent) in its own org."""
    kind, actor_id = actor_ref(actor)
    return await append_audit(
        session,
        action=action,
        actor_type=kind,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=workflow,
        payload=payload,
        org_id=actor.org_id,
    )


async def record_membership_change(
    session: AsyncSession,
    actor: Actor,
    *,
    action: str,
    membership_id: uuid.UUID,
    user_id: uuid.UUID,
    role_key: str,
    workflow: str | None,
    previous_role_key: str | None = None,
) -> AuditRecord:
    """Audit a membership grant, revoke or role change. Ids and role keys only, no PII."""
    if action not in {
        AuditAction.MEMBERSHIP_GRANTED,
        AuditAction.MEMBERSHIP_REVOKED,
        AuditAction.MEMBERSHIP_CHANGED,
    }:
        raise AuditError("not a membership action")
    payload: dict[str, Any] = {"user_id": str(user_id), "role": role_key, "workflow": workflow}
    if previous_role_key is not None:
        payload["previous_role"] = previous_role_key
    return await audit_actor_action(
        session,
        actor,
        action,
        entity_type="membership",
        entity_id=membership_id,
        payload=payload,
    )


async def record_denial(session: AsyncSession, denial: Denial | PermissionDeniedError) -> None:
    """Audit a refused ``authorize`` call, in the session's current transaction.

    Call it after rolling back the denied work, in a fresh transaction, so the denial is
    kept even though the action was not. The HTTP request hook does this automatically.
    """
    info = denial.denial if isinstance(denial, PermissionDeniedError) else denial
    if info is None:
        return
    payload: dict[str, Any] = {"permission": info.permission, "reason": info.reason}
    await audit_actor_action(
        session,
        info.actor,
        AuditAction.AUTHZ_DENIED,
        entity_type=_entity_type_from_class(info.resource_type),
        entity_id=info.resource_id,
        payload=payload,
    )


def _entity_type_from_class(name: str | None) -> str | None:
    if not name:
        return None
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    snake = re.sub(r"[^a-z0-9_]", "", snake)[:64]
    return snake if _ENTITY_TYPE_RE.fullmatch(snake) else None


# ------------------------------------------------------------------ per-transaction tracking


def events_in_transaction(session: AsyncSession | Session) -> int:
    """Audit events appended in the session's current transaction."""
    return int(session.info.get(_INFO_EVENTS, 0))


def orm_writes_in_transaction(session: AsyncSession | Session) -> bool:
    """Whether the current transaction flushed ORM changes to any non-audit row."""
    return bool(session.info.get(_INFO_WRITES, False))


def _track_writes(session: Session, _ctx: Any, _instances: Any) -> None:
    changed = (
        any(not isinstance(o, AuditEvent) for o in session.new)
        or any(not isinstance(o, AuditEvent) for o in session.deleted)
        or any(not isinstance(o, AuditEvent) and session.is_modified(o) for o in session.dirty)
    )
    if changed:
        session.info[_INFO_WRITES] = True


def _reset_tracking(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(_INFO_EVENTS, None)
        session.info.pop(_INFO_WRITES, None)


def _install_listeners() -> None:
    from sqlalchemy import event

    if not event.contains(TenantSession, "before_flush", _track_writes):
        event.listen(TenantSession, "before_flush", _track_writes)
    if not event.contains(TenantSession, "after_transaction_end", _reset_tracking):
        event.listen(TenantSession, "after_transaction_end", _reset_tracking)


_install_listeners()


# ------------------------------------------------------------------ verification


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Result of verifying one organisation's chain."""

    org_id: uuid.UUID
    ok: bool
    checked: int
    last_seq: int
    last_hash: str
    broken_seq: int | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "org_id": str(self.org_id),
            "ok": self.ok,
            "checked": self.checked,
            "last_seq": self.last_seq,
            "last_hash": self.last_hash,
            "broken_seq": self.broken_seq,
            "reason": self.reason,
        }


@dataclass
class ChainVerifier:
    """Checks events of one organisation fed in ``seq`` order. Stops at the first break."""

    org_id: uuid.UUID
    expected_seq: int = 1
    prev_hash: str = GENESIS_HASH
    checked: int = 0
    broken_seq: int | None = None
    reason: str | None = None
    # Anchored heads (seq -> hash) signed outside the database (``audit_anchor``).
    anchors: Mapping[int, str] = field(default_factory=dict)
    _done: bool = field(default=False, repr=False)

    def feed(self, record: AuditRecord) -> bool:
        """Check the next event. Returns False once the chain is broken."""
        if self._done:
            return False
        failure = self._check(record)
        if failure is not None:
            self.broken_seq = self.expected_seq if failure == "missing_event" else record.seq
            self.reason = failure
            self._done = True
            return False
        self.checked += 1
        self.expected_seq += 1
        self.prev_hash = record.hash
        return True

    def _check(self, record: AuditRecord) -> str | None:
        if record.org_id != self.org_id:
            return "org_mismatch"
        if record.seq > self.expected_seq:
            return "missing_event"
        if record.seq != self.expected_seq:
            return "seq_out_of_order"
        if record.prev_hash != self.prev_hash:
            return "prev_hash_mismatch"
        try:
            recomputed = compute_hash(record.prev_hash, record.hashed_fields())
        except AuditError:
            return "unhashable_event"
        if recomputed != record.hash:
            return "hash_mismatch"
        anchored = self.anchors.get(record.seq)
        if anchored is not None and anchored != record.hash:
            return "anchor_mismatch"
        return None

    def result(self) -> ChainVerification:
        broken_seq, reason = self.broken_seq, self.reason
        last_seq = self.expected_seq - 1
        if broken_seq is None and self.anchors and max(self.anchors) > last_seq:
            # The chain ends before a head that was anchored: events were removed.
            broken_seq, reason = last_seq + 1, "anchor_truncated"
        return ChainVerification(
            org_id=self.org_id,
            ok=broken_seq is None,
            checked=self.checked,
            last_seq=last_seq,
            last_hash=self.prev_hash,
            broken_seq=broken_seq,
            reason=reason,
        )


def verify_records(
    org_id: uuid.UUID,
    records: Iterable[AuditRecord],
    *,
    anchors: Mapping[int, str] | None = None,
) -> ChainVerification:
    """Verify a full chain from memory, for example a JSON export, starting at ``seq`` 1.

    ``anchors``: verified anchored heads (seq -> hash) the chain must still contain.
    """
    verifier = ChainVerifier(org_id, anchors=dict(anchors or {}))
    for record in records:
        if not verifier.feed(record):
            break
    return verifier.result()


_RECORD_COLUMNS = (
    AuditEvent.id,
    AuditEvent.org_id,
    AuditEvent.seq,
    AuditEvent.occurred_at,
    AuditEvent.actor_type,
    AuditEvent.actor_id,
    AuditEvent.action,
    AuditEvent.entity_type,
    AuditEvent.entity_id,
    AuditEvent.workflow,
    AuditEvent.payload,
    AuditEvent.request_id,
    AuditEvent.prev_hash,
    AuditEvent.hash,
)


def select_records() -> Select[Any]:
    """A SELECT of every stored column, for building filtered audit queries."""
    return select(*_RECORD_COLUMNS)


async def verify_chain(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    batch_size: int = 1000,
    anchors: Mapping[int, str] | None = None,
) -> ChainVerification:
    """Recompute the whole chain of ``org_id`` and report the first broken ``seq``.

    The session must be bound to ``org_id`` (Row Level Security would otherwise show an
    empty chain, which would wrongly verify). ``anchors``: verified anchored heads
    (seq -> hash, see ``pairly_core.audit_anchor``) the chain must still contain; a
    chain that lost an anchored head fails with ``anchor_mismatch`` or
    ``anchor_truncated``.
    """
    if current_org(session) != org_id:
        raise TenantError("verify_chain needs a session bound to that organisation")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    verifier = ChainVerifier(org_id, anchors=dict(anchors or {}))
    after = 0
    while True:
        rows = (
            await session.execute(
                select_records()
                .where(AuditEvent.org_id == org_id, AuditEvent.seq > after)
                .order_by(AuditEvent.seq)
                .limit(batch_size)
            )
        ).all()
        for row in rows:
            if not verifier.feed(AuditRecord.from_row(row)):
                return verifier.result()
        if len(rows) < batch_size:
            return verifier.result()
        after = int(rows[-1].seq)


# ------------------------------------------------------------------ queries and exports


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Filters shared by the list and export endpoints. Every field is optional."""

    action: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    workflow: str | None = None
    request_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None

    def clauses(self) -> list[ColumnElement[bool]]:
        result: list[ColumnElement[bool]] = []
        exact: Sequence[tuple[Any, str | None]] = (
            (AuditEvent.action, self.action),
            (AuditEvent.actor_type, self.actor_type),
            (AuditEvent.actor_id, self.actor_id),
            (AuditEvent.entity_type, self.entity_type),
            (AuditEvent.entity_id, self.entity_id),
            (AuditEvent.workflow, self.workflow),
            (AuditEvent.request_id, self.request_id),
        )
        for column, value in exact:
            if value is not None:
                result.append(column == value)
        if self.occurred_from is not None:
            result.append(AuditEvent.occurred_at >= self.occurred_from)
        if self.occurred_to is not None:
            result.append(AuditEvent.occurred_at < self.occurred_to)
        return result


CSV_COLUMNS: Final = (
    "seq",
    "id",
    "org_id",
    "occurred_at",
    "actor_type",
    "actor_id",
    "action",
    "entity_type",
    "entity_id",
    "workflow",
    "payload",
    "request_id",
    "prev_hash",
    "hash",
)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formulas (CSV injection) by prefixing a single quote."""
    return "'" + value if value.startswith(_CSV_FORMULA_PREFIXES) else value


def csv_row(record: AuditRecord) -> list[str]:
    """One CSV row. ``payload`` is its canonical JSON. Text cells are formula-safe, so
    the JSON export (not the CSV) is the format to re-verify hashes from."""
    data = record.to_json()
    data["payload"] = canonical_json(data["payload"]).decode("utf-8")
    return [csv_safe("" if data[c] is None else str(data[c])) for c in CSV_COLUMNS]

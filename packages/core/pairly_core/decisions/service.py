# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Write and read decision records (brief 2.3.1).

``record_decision`` is the one way to write a record. It runs in the caller's
transaction, so a record commits or rolls back with the step it records, and:

1. checks the tenant context, the references and that every public part is plain JSON
   (personal data goes in ``sensitive``, which is encrypted);
2. links the record to the one it supersedes. Records of a person's action
   (``HUMAN_KINDS``) supersede the newest record of the same entity that nothing
   supersedes yet, unless the caller names one. A record is superseded at most once
   (unique index), so the history of an entity is one line. A per-entity advisory lock
   serialises writers;
3. computes ``content_hash``: SHA-256 of the canonical JSON of every field, the
   decrypted ``sensitive`` included (``record_content``);
4. inserts the row and appends ``core.decision.recorded`` (decision id, kind, content
   hash) to the audit chain, so a record changed behind the database's back no longer
   matches the chain (``verify_record``).

Records are never updated. A person acting on a decision writes a new record.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import and_, exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from pairly_core.audit import ActorType, append_audit, format_timestamp
from pairly_core.crypto.types import seal, unseal
from pairly_core.db import TenantError, current_org
from pairly_core.decisions.ids import is_decision_id, new_decision_id
from pairly_core.decisions.models import HUMAN_KINDS, DecisionKind, DecisionRecord
from pairly_core.decisions.serialize import EncodingError, canonical, plain_json, sha256_hex
from pairly_core.models.audit import AuditEvent
from pairly_core.models.llm import ModelCall

DECISION_RECORDED: Final = "core.decision.recorded"
MAX_PUBLIC_BYTES: Final = 256 * 1024
MAX_SENSITIVE_BYTES: Final = 512 * 1024

_WORKFLOW_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")
_HANDLER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_.]{0,31}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_:.-]{0,63}$")


class DecisionError(ValueError):
    """A decision record is malformed (a programming error, never user input)."""


class _Auto:
    """Sentinel: let ``record_decision`` choose what the record supersedes."""

    def __repr__(self) -> str:
        return "AUTO"


AUTO: Final = _Auto()


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """One policy version a decision applied (``policy_versions`` row)."""

    id: uuid.UUID
    kind: str
    name: str
    version: int
    hash: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
        }


@dataclass(frozen=True, slots=True)
class PromptRef:
    task: str
    version: str
    hash: str | None

    def as_json(self) -> dict[str, Any]:
        return {"task": self.task, "version": self.version, "hash": self.hash}


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """Everything a decision record holds. See the module docstring."""

    kind: DecisionKind
    workflow: str
    entity_type: str
    entity_id: str
    actor_type: ActorType
    actor_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    policy_versions: Sequence[PolicyRef] = ()
    rubric_version_id: uuid.UUID | None = None
    prompt_versions: Sequence[PromptRef] = ()
    model_call_ids: Sequence[uuid.UUID] = ()
    model: Mapping[str, Any] = field(default_factory=dict)
    output: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Personal data (field snapshot, facts, quotes). Encrypted at rest.
    sensitive: Mapping[str, Any] | None = None
    final_action: str | None = None
    # The person's own words (an internal note). Stored encrypted, inside ``sensitive``.
    reason: str | None = None
    decided_at: datetime | None = None
    audit_event_ids: Sequence[uuid.UUID] = ()
    approval_ids: Sequence[uuid.UUID] = ()
    supersedes: str | _Auto | None = AUTO
    replay_handler: str | None = None


@dataclass(frozen=True, slots=True)
class StoredDecision:
    id: str
    content_hash: str
    supersedes: str | None
    audit_event_id: uuid.UUID


def _check(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DecisionError(f"invalid {name}")
    return value


def _json_part(name: str, value: Any, limit: int) -> Any:
    try:
        clean = plain_json(value)
    except EncodingError as exc:
        raise DecisionError(f"{name}: {exc}") from None
    if len(canonical(clean)) > limit:
        raise DecisionError(f"{name} is larger than {limit} bytes")
    return clean


def record_content(
    *,
    record_id: str,
    org_id: uuid.UUID,
    workflow: str,
    entity_type: str,
    entity_id: str,
    kind: str,
    replay_handler: str | None,
    inputs: Any,
    policy_versions: Any,
    rubric_version_id: uuid.UUID | None,
    prompt_versions: Any,
    model_call_ids: Sequence[uuid.UUID],
    model: Any,
    output: Any,
    confidence: float | None,
    evidence: Any,
    sensitive: Any,
    final_action: str | None,
    actor_type: str,
    actor_id: str,
    decided_at: datetime,
    audit_event_ids: Sequence[uuid.UUID],
    approval_ids: Sequence[uuid.UUID],
    supersedes: str | None,
) -> dict[str, Any]:
    """THE definition of what ``content_hash`` covers (plain JSON, decrypted)."""
    return {
        "id": record_id,
        "org_id": str(org_id),
        "workflow": workflow,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "kind": kind,
        "replay_handler": replay_handler,
        "inputs": inputs,
        "policy_versions": policy_versions,
        "rubric_version_id": str(rubric_version_id) if rubric_version_id else None,
        "prompt_versions": prompt_versions,
        "model_call_ids": [str(c) for c in model_call_ids],
        "model": model,
        "output": output,
        "confidence": confidence,
        "evidence": evidence,
        "sensitive": sensitive,
        "final_action": final_action,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "decided_at": format_timestamp(decided_at),
        "audit_event_ids": [str(a) for a in audit_event_ids],
        "approval_ids": [str(a) for a in approval_ids],
        "supersedes": supersedes,
    }


def content_hash(content: Mapping[str, Any]) -> str:
    return sha256_hex(canonical(dict(content)))


def row_content(row: DecisionRecord, sensitive: Any) -> dict[str, Any]:
    """``record_content`` of a stored row, with its already decrypted ``sensitive``."""
    return record_content(
        record_id=row.id,
        org_id=row.org_id,
        workflow=row.workflow,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        kind=row.kind,
        replay_handler=row.replay_handler,
        inputs=row.inputs,
        policy_versions=row.policy_versions,
        rubric_version_id=row.rubric_version_id,
        prompt_versions=row.prompt_versions,
        model_call_ids=list(row.model_call_ids),
        model=row.model,
        output=row.output,
        confidence=row.confidence,
        evidence=row.evidence,
        sensitive=sensitive,
        final_action=row.final_action,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        decided_at=row.decided_at,
        audit_event_ids=list(row.audit_event_ids),
        approval_ids=list(row.approval_ids),
        supersedes=row.supersedes,
    )


async def _lock_entity(
    session: AsyncSession, org: uuid.UUID, entity_type: str, entity_id: str
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pairly.decision:{org}:{entity_type}:{entity_id}"},
    )


async def chain_head(session: AsyncSession, entity_type: str, entity_id: str) -> str | None:
    """The newest record of an entity that no other record supersedes."""
    later = aliased(DecisionRecord)
    return await session.scalar(
        select(DecisionRecord.id)
        .where(
            DecisionRecord.entity_type == entity_type,
            DecisionRecord.entity_id == entity_id,
            ~exists().where(
                and_(later.org_id == DecisionRecord.org_id, later.supersedes == DecisionRecord.id)
            ),
        )
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(1)
    )


async def prompt_refs(session: AsyncSession, call_ids: Sequence[uuid.UUID]) -> list[PromptRef]:
    """The distinct prompt task, version and hash of stored model calls, in call order."""
    if not call_ids:
        return []
    rows = (
        await session.execute(
            select(
                ModelCall.id, ModelCall.task, ModelCall.prompt_version, ModelCall.prompt_hash
            ).where(ModelCall.id.in_(list(call_ids)))
        )
    ).all()
    by_id = {r.id: r for r in rows}
    seen: dict[tuple[str, str, str], PromptRef] = {}
    for call_id in call_ids:
        row = by_id.get(call_id)
        if row is not None:
            key = (row.task, row.prompt_version, row.prompt_hash)
            seen.setdefault(key, PromptRef(row.task, row.prompt_version, row.prompt_hash))
    return list(seen.values())


async def record_decision(session: AsyncSession, draft: DecisionDraft) -> StoredDecision:
    """Store ``draft`` as a new decision record. See the module docstring."""
    org = current_org(session)
    if org is None:
        raise TenantError("decision records need a tenant context")
    kind = DecisionKind(draft.kind)
    _check("workflow", draft.workflow, _WORKFLOW_RE)
    _check("entity_type", draft.entity_type, _ENTITY_TYPE_RE)
    _check("entity_id", draft.entity_id, _REF_RE)
    _check("actor_id", draft.actor_id, _REF_RE)
    actor_type = ActorType(draft.actor_type)
    if draft.replay_handler is not None:
        _check("replay_handler", draft.replay_handler, _HANDLER_RE)
    if draft.final_action is not None:
        _check("final_action", draft.final_action, _ACTION_RE)
    if draft.confidence is not None and not 0.0 <= draft.confidence <= 1.0:
        raise DecisionError("confidence must be within [0, 1]")

    inputs = _json_part("inputs", draft.inputs, MAX_PUBLIC_BYTES)
    output = _json_part("output", draft.output, MAX_PUBLIC_BYTES)
    evidence = _json_part("evidence", draft.evidence, MAX_PUBLIC_BYTES)
    model = _json_part("model", draft.model, MAX_PUBLIC_BYTES)
    policies = [p.as_json() for p in draft.policy_versions]
    prompts = [p.as_json() for p in draft.prompt_versions]
    sensitive_value: dict[str, Any] | None = None
    if draft.sensitive is not None or draft.reason is not None:
        sensitive_value = dict(draft.sensitive or {})
        if draft.reason is not None:
            sensitive_value["reason"] = draft.reason
    sensitive = (
        _json_part("sensitive", sensitive_value, MAX_SENSITIVE_BYTES)
        if sensitive_value is not None
        else None
    )

    await _lock_entity(session, org, draft.entity_type, draft.entity_id)
    supersedes: str | None
    if isinstance(draft.supersedes, _Auto):
        supersedes = (
            await chain_head(session, draft.entity_type, draft.entity_id)
            if kind in HUMAN_KINDS
            else None
        )
    else:
        supersedes = draft.supersedes
        if supersedes is not None:
            if not is_decision_id(supersedes):
                raise DecisionError("invalid supersedes id")
            found = await session.scalar(
                select(DecisionRecord.id).where(DecisionRecord.id == supersedes)
            )
            if found is None:
                raise DecisionError("the superseded record does not exist")

    record_id = new_decision_id()
    decided_at = draft.decided_at or datetime.now(UTC)
    if decided_at.tzinfo is None:
        raise DecisionError("decided_at must be timezone aware")
    call_ids = list(dict.fromkeys(draft.model_call_ids))
    audit_ids = list(dict.fromkeys(draft.audit_event_ids))
    approval_ids = list(dict.fromkeys(draft.approval_ids))
    content = record_content(
        record_id=record_id,
        org_id=org,
        workflow=draft.workflow,
        entity_type=draft.entity_type,
        entity_id=draft.entity_id,
        kind=kind.value,
        replay_handler=draft.replay_handler,
        inputs=inputs,
        policy_versions=policies,
        rubric_version_id=draft.rubric_version_id,
        prompt_versions=prompts,
        model_call_ids=call_ids,
        model=model,
        output=output,
        confidence=draft.confidence,
        evidence=evidence,
        sensitive=sensitive,
        final_action=draft.final_action,
        actor_type=actor_type.value,
        actor_id=draft.actor_id,
        decided_at=decided_at,
        audit_event_ids=audit_ids,
        approval_ids=approval_ids,
        supersedes=supersedes,
    )
    digest = content_hash(content)
    await session.execute(
        pg_insert(DecisionRecord).values(
            id=record_id,
            org_id=org,
            workflow=draft.workflow,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            kind=kind.value,
            replay_handler=draft.replay_handler,
            inputs=inputs,
            policy_versions=policies,
            rubric_version_id=draft.rubric_version_id,
            prompt_versions=prompts,
            model_call_ids=call_ids,
            model=model,
            output=output,
            confidence=draft.confidence,
            evidence=evidence,
            sensitive=(
                seal(DecisionRecord, "sensitive", sensitive, org_id=org, row_id=record_id)
                if sensitive is not None
                else None
            ),
            final_action=draft.final_action,
            actor_type=actor_type.value,
            actor_id=draft.actor_id,
            decided_at=decided_at,
            audit_event_ids=audit_ids,
            approval_ids=approval_ids,
            supersedes=supersedes,
            content_hash=digest,
        )
    )
    event = await append_audit(
        session,
        action=DECISION_RECORDED,
        actor_type=actor_type,
        actor_id=draft.actor_id,
        entity_type="decision_record",
        entity_id=record_id,
        workflow=draft.workflow,
        payload={
            "kind": kind.value,
            "subject_type": draft.entity_type,
            "subject_id": draft.entity_id,
            "content_hash": digest,
            "supersedes": supersedes,
        },
    )
    return StoredDecision(record_id, digest, supersedes, event.id)


# ------------------------------------------------------------------ reads


async def get_record(session: AsyncSession, decision_id: str) -> DecisionRecord | None:
    """One record of the session's organisation (another org's id reads as None)."""
    if not is_decision_id(decision_id):
        return None
    return await session.scalar(select(DecisionRecord).where(DecisionRecord.id == decision_id))


def open_sensitive(row: DecisionRecord) -> Any:
    """The decrypted ``sensitive`` part (raises ``DecryptionError`` when tampered)."""
    return unseal(row, "sensitive")


async def superseded_by(session: AsyncSession, decision_id: str) -> str | None:
    return await session.scalar(
        select(DecisionRecord.id).where(DecisionRecord.supersedes == decision_id)
    )


async def history(session: AsyncSession, row: DecisionRecord, *, limit: int = 50) -> list[str]:
    """Ids of the record's line, oldest first: what it superseded, it, what superseded it."""
    before: list[str] = []
    current = row.supersedes
    while current is not None and len(before) < limit:
        before.append(current)
        current = await session.scalar(
            select(DecisionRecord.supersedes).where(DecisionRecord.id == current)
        )
    after: list[str] = []
    nxt = await superseded_by(session, row.id)
    while nxt is not None and len(after) < limit:
        after.append(nxt)
        nxt = await superseded_by(session, nxt)
    return [*reversed(before), row.id, *after]


async def recorded_hash(session: AsyncSession, decision_id: str) -> str | None:
    """The ``content_hash`` the audit chain holds for this record."""
    payload = await session.scalar(
        select(AuditEvent.payload)
        .where(
            AuditEvent.action == DECISION_RECORDED,
            AuditEvent.entity_type == "decision_record",
            AuditEvent.entity_id == decision_id,
        )
        .order_by(AuditEvent.seq)
        .limit(1)
    )
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("content_hash")
    return value if isinstance(value, str) else None


async def verify_record(session: AsyncSession, row: DecisionRecord) -> str | None:
    """``None`` when the record is intact, else why not (fail closed).

    Intact means: ``sensitive`` decrypts, the recomputed content hash equals the stored
    one, and both equal the hash written to the audit chain when the record was made.
    """
    from pairly_core.crypto.envelope import DecryptionError

    try:
        sensitive = open_sensitive(row)
    except (DecryptionError, ValueError, TypeError):
        return "decryption_failed"
    recomputed = content_hash(row_content(row, sensitive))
    if recomputed != row.content_hash:
        return "content_hash_mismatch"
    if await recorded_hash(session, row.id) != row.content_hash:
        return "audit_hash_mismatch"
    return None

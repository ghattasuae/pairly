# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Store extractions, record corrections (append only) and serve few-shot hints.

Every write here is audited in the same transaction. Corrections never change an
extraction or a field row: each one is a new ``extraction_corrections`` row, and the
current value of a field is its latest correction (else the extracted value). The
"old value" of a correction is computed here from that history under a per-extraction
lock, never taken from the client.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, actor_ref, append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.decisions.recorders import on_extraction_corrected, on_extraction_saved
from pairly_core.errors import BadRequestError, NotFoundError
from pairly_core.extraction.engine import ExtractionResult, FewShotHint
from pairly_core.extraction.models import Extraction, ExtractionCorrection, ExtractionField
from pairly_core.extraction.permissions import authorize_extraction
from pairly_core.extraction.schema import StoredSpecs, schema_workflow
from pairly_core.extraction.types import json_value
from pairly_core.extraction.validators import InvalidValue, parse_value
from pairly_core.ingestion.models import Document
from pairly_core.rbac import Actor, ActorKind, RoleRegistry

EXTRACTION_CREATED = "core.extraction.created"
EXTRACTION_CORRECTED = "core.extraction.corrected"
MAX_REASON = 1000
MAX_NEW_VALUE = 2000
_TEMPLATE_KEY_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class StoredField:
    path: str
    pattern: str
    kind: str
    value: Any
    raw_value: str | None
    confidence: float
    threshold: float
    required: bool
    needs_correction: bool
    flags: list[str]
    spans: list[dict[str, Any]]
    position: int


@dataclass(frozen=True, slots=True)
class StoredCorrection:
    id: uuid.UUID
    extraction_id: uuid.UUID
    field_path: str
    old_value: Any
    new_value: Any
    corrected_by: uuid.UUID
    reason: str
    created_at: datetime

    @classmethod
    def of(cls, row: ExtractionCorrection) -> StoredCorrection:
        return cls(
            id=row.id,
            extraction_id=row.extraction_id,
            field_path=row.field_path,
            old_value=row.old_value,
            new_value=row.new_value,
            corrected_by=row.corrected_by,
            reason=row.reason,
            created_at=row.created_at,
        )


@dataclass(frozen=True, slots=True)
class StoredExtraction:
    row: Extraction
    fields: list[StoredField]
    corrections: list[StoredCorrection]

    @property
    def org_id(self) -> uuid.UUID:
        return self.row.org_id

    @property
    def id(self) -> uuid.UUID:
        return self.row.id

    def current_values(self) -> dict[str, Any]:
        """Each field's current value: its latest correction, else the extracted value."""
        values = {f.path: f.value for f in self.fields}
        for correction in self.corrections:  # oldest first
            values[correction.field_path] = correction.new_value
        return values


def _check_template_key(key: str | None) -> None:
    if key is not None and not _TEMPLATE_KEY_RE.fullmatch(key):
        raise ValueError("invalid template_key")


async def save_extraction(
    session: AsyncSession,
    result: ExtractionResult[Any],
    *,
    document_id: uuid.UUID,
    actor_type: ActorType,
    actor_id: str,
    template_key: str | None = None,
) -> uuid.UUID:
    """Store ``result`` for ``document_id`` with an audit event. Returns the new id.

    The document must be visible in the session's organisation, and the schema must
    belong to the document's workflow (or to ``core``).
    """
    org = current_org(session)
    if org is None:
        raise TenantError("extractions need a tenant context")
    _check_template_key(template_key)
    document = await session.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        raise NotFoundError("document not found")
    owner = schema_workflow(result.schema)
    if owner not in {document.workflow, "core"}:
        raise ValueError("the extraction schema belongs to another workflow")
    extraction_id = uuid.uuid4()
    call = result.model_call
    await session.execute(
        pg_insert(Extraction).values(
            id=extraction_id,
            org_id=org,
            document_id=document_id,
            workflow=document.workflow,
            schema_name=result.schema.name,
            schema_version=result.schema.version,
            field_specs=result.schema.field_specs(),
            prompt_task=call.prompt_task,
            prompt_version=call.prompt_version,
            prompt_hash=call.prompt_hash,
            model_provider=call.provider,
            model_name=call.model,
            model_call_ids=list(call.call_ids),
            model_chain_id=call.chain_id,
            from_recording=call.from_recording,
            status=result.status.value,
            reasons=list(result.reasons),
            ignored_paths=list(result.ignored_paths),
            template_key=template_key,
            created_by_type=actor_type.value,
            created_by_id=actor_id,
        )
    )
    if result.fields:
        await session.execute(
            pg_insert(ExtractionField),
            [
                {
                    "org_id": org,
                    "extraction_id": extraction_id,
                    "position": position,
                    "path": item.path,
                    "pattern": item.pattern,
                    "kind": item.kind,
                    "value": item.json_value,
                    "raw_value": item.raw,
                    "confidence": item.confidence,
                    "threshold": item.threshold,
                    "required": item.required,
                    "needs_correction": item.needs_correction,
                    "flags": [f.value for f in item.flags],
                    "spans": [s.model_dump(mode="json") for s in item.spans],
                }
                for position, item in enumerate(result.fields)
            ],
        )
    event = await append_audit(
        session,
        action=EXTRACTION_CREATED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="extraction",
        entity_id=extraction_id,
        workflow=document.workflow,
        payload={
            "document_id": str(document_id),
            "schema": result.schema.name,
            "schema_version": result.schema.version,
            "prompt_version": call.prompt_version,
            "status": result.status.value,
            "reasons": list(result.reasons[:50]),
            "model_call_ids": [str(c) for c in call.call_ids],
        },
    )
    await on_extraction_saved(
        session,
        extraction_id=extraction_id,
        document=document,
        result=result,
        actor_type=actor_type,
        actor_id=actor_id,
        template_key=template_key,
        audit_event_id=event.id,
    )
    return extraction_id


async def load_extraction(
    session: AsyncSession, extraction_id: uuid.UUID
) -> StoredExtraction | None:
    row = await session.scalar(select(Extraction).where(Extraction.id == extraction_id))
    if row is None:
        return None
    fields = (
        await session.scalars(
            select(ExtractionField)
            .where(ExtractionField.extraction_id == extraction_id)
            .order_by(ExtractionField.position)
        )
    ).all()
    corrections = (
        await session.scalars(
            select(ExtractionCorrection)
            .where(ExtractionCorrection.extraction_id == extraction_id)
            .order_by(ExtractionCorrection.created_at, ExtractionCorrection.id)
        )
    ).all()
    return StoredExtraction(
        row=row,
        fields=[
            StoredField(
                path=f.path,
                pattern=f.pattern,
                kind=f.kind,
                value=f.value,
                raw_value=f.raw_value,
                confidence=f.confidence,
                threshold=f.threshold,
                required=f.required,
                needs_correction=f.needs_correction,
                flags=list(f.flags),
                spans=list(f.spans),
                position=f.position,
            )
            for f in fields
        ],
        corrections=[StoredCorrection.of(c) for c in corrections],
    )


def _clean_reason(reason: str) -> str:
    cleaned = _CONTROL_RE.sub("", reason or "").strip()
    if not cleaned:
        raise BadRequestError("a reason is required")
    if len(cleaned) > MAX_REASON:
        raise BadRequestError(f"the reason is longer than {MAX_REASON} characters")
    return cleaned


async def correct_field(
    session: AsyncSession,
    actor: Actor,
    registry: RoleRegistry,
    extraction_id: uuid.UUID,
    *,
    field_path: str,
    new_value: str | None,
    reason: str,
) -> StoredCorrection:
    """Record a person's correction of one field. Returns the stored correction.

    ``new_value`` is text, parsed and checked with the field's rules as stored with the
    extraction; ``None`` clears the field. Raises ``NotFoundError`` (unknown extraction,
    including another organisation's), ``PermissionDeniedError`` and ``BadRequestError``.
    """
    if actor.kind is not ActorKind.USER:
        # Belt and braces: ``correct`` is never an agent permission anyway.
        raise BadRequestError("only a person can correct an extraction")
    row = await session.scalar(select(Extraction).where(Extraction.id == extraction_id))
    if row is None:
        raise NotFoundError("extraction not found")
    authorize_extraction(actor, registry, workflow=row.workflow, action="correct", resource=row)
    reason = _clean_reason(reason)
    resolved = StoredSpecs.from_json(row.field_specs).value_spec(field_path)
    if resolved is None:
        raise BadRequestError("unknown field path for this extraction")
    pattern, spec = resolved
    if new_value is None:
        typed: Any = None
    else:
        if len(new_value) > MAX_NEW_VALUE:
            raise BadRequestError("the new value is too long")
        try:
            typed = json_value(parse_value(spec, new_value))
        except InvalidValue as exc:
            raise BadRequestError(f"the new value is not valid: {exc}") from None

    # Serialise corrections of one extraction so "old value" is always the current one.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pairly.extraction:{row.org_id}:{row.id}"},
    )
    old = await _current_value(session, row.id, field_path)
    if old == typed:
        raise BadRequestError("the new value equals the current value")
    correction_id = uuid.uuid4()
    await session.execute(
        pg_insert(ExtractionCorrection).values(
            id=correction_id,
            org_id=row.org_id,
            extraction_id=row.id,
            field_path=field_path,
            field_pattern=pattern,
            old_value=old,
            new_value=typed,
            corrected_by=actor.id,
            reason=reason,
            workflow=row.workflow,
            schema_name=row.schema_name,
            template_key=row.template_key,
        )
    )
    kind, actor_id = actor_ref(actor)
    event = await append_audit(
        session,
        action=EXTRACTION_CORRECTED,
        actor_type=kind,
        actor_id=actor_id,
        entity_type="extraction",
        entity_id=row.id,
        workflow=row.workflow,
        payload={"correction_id": str(correction_id), "field_path": field_path},
    )
    await on_extraction_corrected(
        session,
        extraction_id=row.id,
        document_id=row.document_id,
        workflow=row.workflow,
        correction_id=correction_id,
        field_path=field_path,
        old_value=old,
        new_value=typed,
        reason=reason,
        actor_id=actor.id,
        audit_event_id=event.id,
    )
    stored = await session.scalar(
        select(ExtractionCorrection).where(ExtractionCorrection.id == correction_id)
    )
    if stored is None:  # pragma: no cover - RLS would have refused the insert
        raise TenantError("correction not visible after insert")
    return StoredCorrection.of(stored)


async def _current_value(session: AsyncSession, extraction_id: uuid.UUID, path: str) -> Any:
    latest = await session.scalar(
        select(ExtractionCorrection.new_value)
        .where(
            ExtractionCorrection.extraction_id == extraction_id,
            ExtractionCorrection.field_path == path,
        )
        .order_by(ExtractionCorrection.created_at.desc(), ExtractionCorrection.id.desc())
        .limit(1)
    )
    if latest is not None:
        return latest
    has_correction = await session.scalar(
        select(ExtractionCorrection.id)
        .where(
            ExtractionCorrection.extraction_id == extraction_id,
            ExtractionCorrection.field_path == path,
        )
        .limit(1)
    )
    if has_correction is not None:
        return None  # latest correction cleared the field
    return await session.scalar(
        select(ExtractionField.value).where(
            ExtractionField.extraction_id == extraction_id, ExtractionField.path == path
        )
    )


async def few_shot_examples(
    session: AsyncSession,
    *,
    workflow: str,
    schema_name: str,
    template_key: str,
    limit: int = 20,
) -> list[FewShotHint]:
    """Recent corrections for one vendor or template, newest first, as prompt hints.

    One hint per (field pattern, corrected value): repeated corrections do not crowd
    out others. Only this organisation's corrections are visible (RLS).
    """
    _check_template_key(template_key)
    limit = max(1, min(limit, 50))
    rows: Sequence[ExtractionCorrection] = (
        await session.scalars(
            select(ExtractionCorrection)
            .where(
                ExtractionCorrection.workflow == workflow,
                ExtractionCorrection.schema_name == schema_name,
                ExtractionCorrection.template_key == template_key,
            )
            .order_by(ExtractionCorrection.created_at.desc(), ExtractionCorrection.id.desc())
            .limit(limit * 4)
        )
    ).all()
    hints: list[FewShotHint] = []
    seen: set[str] = set()
    for row in rows:
        key = f"{row.field_pattern}\x00{row.new_value!r}"
        if key in seen:
            continue
        seen.add(key)
        hints.append(FewShotHint(row.field_pattern, row.old_value, row.new_value))
        if len(hints) >= limit:
            break
    return hints

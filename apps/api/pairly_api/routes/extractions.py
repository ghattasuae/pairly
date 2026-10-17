# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``/v1/extractions``: read an extraction and correct its fields.

* ``GET /v1/extractions/{id}``               the extraction, its fields with evidence,
  the correction history (oldest first) and each field's current value.
* ``POST /v1/extractions/{id}/corrections``  a person corrects one field. Appends a
  correction row (history is never changed) and an audit event. 201 with the row.

Permissions follow ``pairly_core.extraction.permissions``: ``<workflow>.extraction.*``
when the workflow declares it, else ``core.extraction.*``. Another organisation's
extraction is a 404 (Row Level Security hides it).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from pairly_core.errors import NotFoundError
from pairly_core.extraction.permissions import authorize_extraction
from pairly_core.extraction.service import (
    StoredCorrection,
    correct_field,
    load_extraction,
)
from pairly_core.identity import Context, CurrentActor, OrgSession

router = APIRouter(prefix="/extractions", tags=["extractions"])


class FieldOut(BaseModel):
    path: str
    kind: str
    value: Any
    current_value: Any
    raw_value: str | None
    confidence: float
    threshold: float
    required: bool
    needs_correction: bool
    corrected: bool
    flags: list[str]
    spans: list[dict[str, Any]]


class CorrectionOut(BaseModel):
    id: uuid.UUID
    field_path: str
    old_value: Any
    new_value: Any
    corrected_by: uuid.UUID
    reason: str
    created_at: datetime

    @classmethod
    def of(cls, c: StoredCorrection) -> CorrectionOut:
        return cls(
            id=c.id,
            field_path=c.field_path,
            old_value=c.old_value,
            new_value=c.new_value,
            corrected_by=c.corrected_by,
            reason=c.reason,
            created_at=c.created_at,
        )


class ExtractionOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    workflow: str
    schema_name: str
    schema_version: str
    status: str
    reasons: list[str]
    prompt_task: str
    prompt_version: str
    prompt_hash: str | None
    model_provider: str | None
    model_name: str | None
    model_call_ids: list[uuid.UUID]
    from_recording: bool
    template_key: str | None
    created_at: datetime
    fields: list[FieldOut]
    corrections: list[CorrectionOut]
    # Paths a person corrected that the model never produced (for example a new line item).
    extra_values: dict[str, Any]


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(min_length=1, max_length=256)
    new_value: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/{extraction_id}", response_model=ExtractionOut)
async def get_extraction(
    extraction_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> ExtractionOut:
    stored = await load_extraction(session, extraction_id)
    if stored is None:
        raise NotFoundError("extraction not found")
    row = stored.row
    authorize_extraction(actor, ctx.registry, workflow=row.workflow, action="read", resource=row)
    current = stored.current_values()
    corrected = {c.field_path for c in stored.corrections}
    known = {f.path for f in stored.fields}
    return ExtractionOut(
        id=row.id,
        document_id=row.document_id,
        workflow=row.workflow,
        schema_name=row.schema_name,
        schema_version=row.schema_version,
        status=row.status,
        reasons=list(row.reasons),
        prompt_task=row.prompt_task,
        prompt_version=row.prompt_version,
        prompt_hash=row.prompt_hash,
        model_provider=row.model_provider,
        model_name=row.model_name,
        model_call_ids=list(row.model_call_ids),
        from_recording=row.from_recording,
        template_key=row.template_key,
        created_at=row.created_at,
        fields=[
            FieldOut(
                path=f.path,
                kind=f.kind,
                value=f.value,
                current_value=current.get(f.path),
                raw_value=f.raw_value,
                confidence=f.confidence,
                threshold=f.threshold,
                required=f.required,
                needs_correction=f.needs_correction and f.path not in corrected,
                corrected=f.path in corrected,
                flags=f.flags,
                spans=f.spans,
            )
            for f in stored.fields
        ],
        corrections=[CorrectionOut.of(c) for c in stored.corrections],
        extra_values={p: v for p, v in current.items() if p not in known},
    )


@router.post("/{extraction_id}/corrections", status_code=201, response_model=CorrectionOut)
async def post_correction(
    extraction_id: uuid.UUID,
    body: CorrectionIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> CorrectionOut:
    correction = await correct_field(
        session,
        actor,
        ctx.registry,
        extraction_id,
        field_path=body.field_path,
        new_value=body.new_value,
        reason=body.reason,
    )
    return CorrectionOut.of(correction)

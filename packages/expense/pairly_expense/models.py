# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``expense_notes`` table (migration ``expense_0001``). Org scoped, RLS forced.

One row per submitted expense note: who submitted it, the receipt document, the run that
processes it, what was read from the receipt and the outcome of every check, and the
approval and decision records behind its status.

Status (``NoteStatus``)::

    processing --(checks done, approval requested)--> awaiting_approval
    awaiting_approval --> approved | rejected | changes_requested
    any --(the run could not continue)--> failed

``approved`` is set only by the ``mark_reimbursable`` final action, which the executor
runs after re-checking the approval. Nothing here moves money: ``reimbursable`` is a
flag for finance. Amounts are ``Numeric``, never floats.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

TABLE = "expense_notes"
MAX_NOTE = 1000


class NoteStatus(StrEnum):
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    FAILED = "failed"


FINAL_STATUSES = frozenset(
    {NoteStatus.APPROVED, NoteStatus.REJECTED, NoteStatus.CHANGES_REQUESTED, NoteStatus.FAILED}
)


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class ExpenseNote(Base, OrgScoped):
    __tablename__ = TABLE
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(NoteStatus)})", name="status_valid"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount_non_negative"),
        CheckConstraint("amount_aed IS NULL OR amount_aed >= 0", name="amount_aed_non_negative"),
        CheckConstraint(f"char_length(note) <= {MAX_NOTE}", name="note_length"),
        # Only the final action marks a note reimbursable, and only an approved one.
        CheckConstraint("NOT reimbursable OR status = 'approved'", name="reimbursable_approved"),
        # Same organisation as the note, enforced by the key itself (FK checks ignore RLS).
        ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_expense_notes_extraction_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "risk_assessment_id"],
            ["risk_assessments.org_id", "risk_assessments.id"],
            ondelete="RESTRICT",
            name="fk_expense_notes_risk_assessment_org",
        ),
        Index("ix_expense_notes_submitter", "org_id", "submitted_by", "created_at"),
        Index("ix_expense_notes_status", "org_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # The submitter's own words. Shown to approvers, never sent to a model.
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=NoteStatus.PROCESSING.value
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    rules_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    risk_assessment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    # What was read from the receipt (current values, corrections included).
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # The amount in AED when the receipt is in AED (no exchange rates in this example).
    amount_aed: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Why a person must look closely (fail closed reasons, flags, findings).
    review_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # The guardrail report of the receipt (pairly_core.guardrails.report), for reviewers.
    guardrail_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Digest of the sanitised text the model saw (binds the extract step to the sanitise
    # step) and how many spans the guardrails removed from it.
    sanitized_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    removed_spans: Mapped[list[dict[str, int]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    decision_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reimbursable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

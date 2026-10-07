# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Expense note helpers shared by the graph, the final action and the routes.

* ``locked_note`` / ``add_reasons`` / ``refresh_decision_ids``: small row operations.
* ``ReceiptValues`` / ``receipt_values``: what was read from the receipt, keeping only
  values a check may rely on (evidenced and confident, or corrected by a person).
* ``approval_context``: the approval's context in the inbox convention.
* ``restore_result``: an ``ExtractionResult`` rebuilt from the stored extraction, so the
  risk step reads exactly what the extraction step stored (graph state only carries
  ids, never Python objects).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import set_tenant
from pairly_core.decisions.models import DecisionKind, DecisionRecord
from pairly_core.errors import PairlyError
from pairly_core.extraction.engine import (
    ExtractionResult,
    ExtractionStatus,
    FieldResult,
    ModelCallInfo,
)
from pairly_core.extraction.schema import compile_schema
from pairly_core.extraction.service import StoredExtraction
from pairly_core.extraction.types import FieldFlag, SourceSpan
from pairly_core.guardrails.report import GuardrailReport
from pairly_core.identity import load_actor
from pairly_core.models import User
from pairly_core.rbac import Actor, RoleRegistry
from pairly_expense.models import ExpenseNote
from pairly_expense.permissions import NOTE_SUBMIT
from pairly_expense.schema import ExpenseReceipt

WORKFLOW: Final = "expense"
ENTITY_TYPE: Final = "expense_note"
MAX_REASONS: Final = 50
# Reasons ``extract`` gives when it could not read the receipt at all (no data).
_FAILED_EXTRACTION: Final = frozenset({"no_text", "model_unavailable", "data_validation_failed"})
# The UAE weekend.
WEEKEND: Final = frozenset({5, 6})


class NoteMissingError(LookupError):
    """The note of a run is gone or belongs to another organisation."""


async def locked_note(session: AsyncSession, note_id: uuid.UUID) -> ExpenseNote:
    note = await session.scalar(
        select(ExpenseNote)
        .where(ExpenseNote.id == note_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if note is None:
        raise NoteMissingError("expense note not found")
    return note


def add_reasons(note: ExpenseNote, reasons: Iterable[str]) -> None:
    merged = list(dict.fromkeys([*note.review_reasons, *(r[:200] for r in reasons)]))
    note.review_reasons = merged[:MAX_REASONS]


async def refresh_decision_ids(session: AsyncSession, note: ExpenseNote) -> None:
    """Every decision record about this note (and the extraction of its receipt)."""
    rows = await session.scalars(
        select(DecisionRecord.id)
        .where(
            DecisionRecord.workflow == WORKFLOW,
            or_(
                and_(
                    DecisionRecord.entity_type == ENTITY_TYPE,
                    DecisionRecord.entity_id == str(note.id),
                ),
                and_(
                    DecisionRecord.entity_type == "document",
                    DecisionRecord.entity_id == str(note.document_id),
                    DecisionRecord.kind.in_(
                        [DecisionKind.EXTRACTION.value, DecisionKind.EXTRACTION_CORRECTION.value]
                    ),
                ),
            ),
        )
        .order_by(DecisionRecord.decided_at, DecisionRecord.id)
    )
    note.decision_ids = list(rows)


MAX_CONTEXT_EVIDENCE: Final = 40
MAX_CONTEXT_FINDINGS: Final = 40
MAX_CONTEXT_QUOTE: Final = 300
MAX_CONTEXT_BOXES: Final = 8
# The engine refuses an approval context over 32 KiB; keep well under it.
MAX_CONTEXT_BYTES: Final = 28 * 1024


def approval_context(
    note: ExpenseNote,
    stored: StoredExtraction | None,
    report: GuardrailReport | None,
) -> dict[str, Any]:
    """The approval's ``context``, in the inbox convention (apps/web/README.md).

    ``document_id`` (the receipt), ``evidence[]`` (one item per located span of each
    extracted field: ``field``, ``label``, ``quote``, ``page``, ``bboxes``),
    ``guardrail_findings[]`` (``id``, ``type``, ``code``, ``severity``, ``page``,
    ``bbox``, ``evidence``, ``location``) and ``decision_ids[]``, plus the note's own
    keys. Built from stored rows only, bounded in size: quotes and excerpts are cut, and
    when it is still too large the last evidence items, then findings, are left out (the
    full lists stay on the note and its extraction).
    """
    evidence: list[dict[str, Any]] = []
    for item in stored.fields if stored is not None else ():
        for span in item.spans:
            if len(evidence) >= MAX_CONTEXT_EVIDENCE:
                break
            evidence.append(
                {
                    "field": item.path,
                    "label": item.path,
                    "quote": str(span.get("quote") or "")[:MAX_CONTEXT_QUOTE],
                    "page": span.get("page"),
                    "bboxes": list(span.get("bboxes") or [])[:MAX_CONTEXT_BOXES],
                }
            )
    findings = [
        {
            "id": f.id,
            "type": f.type.value,
            "code": f.code,
            "severity": f.severity.value,
            "page": f.page,
            "bbox": f.bbox.model_dump(mode="json") if f.bbox is not None else None,
            "evidence": f.evidence[:MAX_CONTEXT_QUOTE],
            "location": f.location,
        }
        for f in (report.findings if report is not None else ())[:MAX_CONTEXT_FINDINGS]
    ]
    context: dict[str, Any] = {
        "document_id": str(note.document_id),
        "evidence": evidence,
        "guardrail_findings": findings,
        "decision_ids": list(note.decision_ids),
        "review_reasons": list(note.review_reasons),
        "note": note.note,
        "submitted_by": str(note.submitted_by),
    }
    while _json_size(context) > MAX_CONTEXT_BYTES and (evidence or findings):
        (evidence or findings).pop()
    return context


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def submitter_actor(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RoleRegistry,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Actor | None:
    """The submitter as an actor, if they are still a member who may submit."""
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        subject = await session.scalar(select(User.auth_subject).where(User.id == user_id))
    if subject is None:
        return None
    session = sessionmaker()
    async with session:
        try:
            actor = await load_actor(session, registry, subject=subject, org_id=org_id)
        except PairlyError:
            return None
        finally:
            await session.rollback()
    return actor if actor.has(NOTE_SUBMIT) else None


def reset_for_new_run(note: ExpenseNote) -> None:
    """Clear what the previous run produced, so a fresh run starts from the receipt."""
    note.run_id = None
    note.approval_id = None
    note.extraction_id = None
    note.rules_evaluation_id = None
    note.risk_assessment_id = None
    note.merchant = None
    note.expense_date = None
    note.amount = None
    note.currency = None
    note.amount_aed = None
    note.category = None
    note.review_reasons = []
    note.guardrail_report = None
    note.sanitized_sha256 = None
    note.removed_spans = []
    note.failure_reason = None
    note.decided_at = None


# ------------------------------------------------------------------ receipt values


@dataclass(frozen=True, slots=True)
class ReceiptValues:
    """Values a check may rely on; ``None`` when not read, flagged or invalid."""

    merchant: str | None = None
    date: dt.date | None = None
    amount: Decimal | None = None
    currency: str | None = None
    category: str | None = None
    description: str | None = None
    # Merchant, date and total all read with evidence: the receipt backs the claim.
    evidenced: bool = False

    @property
    def amount_aed(self) -> Decimal | None:
        return self.amount if self.currency == "AED" else None

    def facts(self) -> dict[str, Any]:
        """Facts for the ``expense.note`` rule set. Unknown values are left out, so a
        rule that needs one gives ``error`` (a person reviews), never ``pass``."""
        facts: dict[str, Any] = {"receipt_evidenced": self.evidenced}
        if self.amount is not None:
            facts["amount"] = self.amount
        if self.currency is not None:
            facts["currency"] = self.currency
        if self.category is not None:
            facts["category"] = self.category
        if self.date is not None:
            facts["is_weekend"] = self.date.weekday() in WEEKEND
        return facts


def _usable(stored: StoredExtraction) -> dict[str, Any]:
    """Field values that are evidenced and confident, or corrected by a person."""
    usable = {f.path: f.value for f in stored.fields if not f.flags and f.value is not None}
    for correction in stored.corrections:  # oldest first; a person's word wins
        if correction.new_value is None:
            usable.pop(correction.field_path, None)
        else:
            usable[correction.field_path] = correction.new_value
    return usable


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def receipt_values(stored: StoredExtraction | None) -> ReceiptValues:
    if stored is None:
        return ReceiptValues()
    usable = _usable(stored)
    total = usable.get("total")
    amount: Decimal | None = None
    total_currency: str | None = None
    if isinstance(total, dict):
        amount = _decimal(total.get("amount"))
        raw_currency = total.get("currency")
        total_currency = raw_currency if isinstance(raw_currency, str) else None
    currency = usable.get("currency")
    if not isinstance(currency, str):
        currency = total_currency
    elif total_currency is not None and total_currency != currency:
        # The total says one currency and the currency field another: trust neither.
        amount, currency = None, None
    merchant = usable.get("merchant")
    category = usable.get("category")
    description = usable.get("description")
    expense_date = _date(usable["date"]) if "date" in usable else None
    return ReceiptValues(
        merchant=merchant if isinstance(merchant, str) else None,
        date=expense_date,
        amount=amount,
        currency=currency if isinstance(currency, str) else None,
        category=category if isinstance(category, str) else None,
        description=description if isinstance(description, str) else None,
        evidenced=all(k in usable for k in ("merchant", "date", "total")),
    )


# ------------------------------------------------------------------ restoring a result


def restore_result(stored: StoredExtraction) -> ExtractionResult[ExpenseReceipt]:
    """The ``ExtractionResult`` the extraction step produced, rebuilt from its rows."""
    schema = compile_schema(ExpenseReceipt)
    row = stored.row
    fields = tuple(
        FieldResult(
            path=f.path,
            pattern=f.pattern,
            kind=f.kind,
            value=f.value,
            raw=f.raw_value,
            confidence=f.confidence,
            threshold=f.threshold,
            required=f.required,
            flags=tuple(FieldFlag(flag) for flag in f.flags),
            spans=tuple(SourceSpan.model_validate(span) for span in f.spans),
        )
        for f in stored.fields
    )
    reasons = tuple(str(r) for r in row.reasons)
    failed = bool(reasons) and reasons[0].split(":", 1)[0] in _FAILED_EXTRACTION
    data: ExpenseReceipt | None = None
    if not failed:
        data = ExpenseReceipt.model_validate(
            {
                f.path: {
                    "value": f.value,
                    "confidence": f.confidence,
                    "spans": [s.model_dump(mode="json") for s in f.spans],
                    "flags": [flag.value for flag in f.flags],
                    "raw": f.raw,
                }
                for f in fields
            }
        )
    return ExtractionResult(
        schema=schema,
        status=ExtractionStatus(row.status),
        reasons=reasons,
        fields=fields,
        data=data,
        model_call=ModelCallInfo(
            call_ids=tuple(row.model_call_ids),
            chain_id=row.model_chain_id,
            prompt_task=row.prompt_task,
            prompt_version=row.prompt_version,
            prompt_hash=row.prompt_hash,
            provider=row.model_provider,
            model=row.model_name,
            from_recording=row.from_recording,
        ),
        ignored_paths=tuple(row.ignored_paths),
        schema_description=schema.describe(),
    )

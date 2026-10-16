# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bank reconciliation routes (``pairly_pay.reconciliation``).

* ``GET /reconciliation?days=5``  (``pay.reconciliation.read``) the recent statements,
  the unmatched statement lines, the latest matches, and the paid invoices no statement
  line explains ``days`` (1 to 90) after they were marked paid. Every row links to its
  invoice, run or statement.
* ``POST /reconciliation/statements``  (``pay.reconciliation.upload``, a person)
  ``{"format": "csv" | "camt053", "content": "..."}``: import a statement and match it.
  A problem anywhere refuses the whole file (400 with every problem); the same file
  twice is refused (409).
* ``POST /reconciliation/match``  (``pay.reconciliation.upload``, a person): match the
  unmatched lines again now (the hourly job does the same).

Counterparty IBANs are shown masked only. Amounts are strings at the currency's minor
unit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from pairly_api.problems import problem_response
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.api.vendors import person
from pairly_pay.checks.settings import amount_text
from pairly_pay.models import (
    PayBankStatement,
    PayBankStatementLine,
    PayInvoice,
    PayPaymentRun,
    StatementLineStatus,
)
from pairly_pay.permissions import RECONCILIATION_READ, RECONCILIATION_UPLOAD
from pairly_pay.purchase_orders import MAX_CSV_BYTES
from pairly_pay.reconciliation import DEFAULT_DAYS, import_statement, overdue_items, reconcile
from pairly_pay.statements import InvalidStatementError, parse_statement

router = APIRouter(tags=["pay"])

MAX_DAYS: Final = 90
MAX_LISTED: Final = 500
RECENT_STATEMENTS: Final = 20
RECENT_MATCHES: Final = 200


class PayStatementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["csv", "camt053"]
    content: Annotated[str, Field(min_length=1, max_length=MAX_CSV_BYTES)]


class PayStatementOut(BaseModel):
    id: uuid.UUID
    reference: str
    format: str
    file_sha256: str
    bank_statement_id: str | None
    account_masked: str | None
    from_date: date
    to_date: date
    line_count: int
    created_at: datetime


class PayStatementLineOut(BaseModel):
    id: uuid.UUID
    statement_id: uuid.UUID
    statement_reference: str
    line_no: int
    booking_date: date
    amount: str
    currency: str
    reference: str | None
    counterparty_iban_masked: str | None
    status: str
    match_kind: str | None
    unmatched_reason: str | None
    match_reasons: list[str]
    payment_run_id: uuid.UUID | None
    payment_run_reference: str | None
    invoice_id: uuid.UUID | None
    invoice_reference: str | None


class PayUnexplainedOut(BaseModel):
    invoice_id: uuid.UUID
    invoice_reference: str
    invoice_number: str | None
    vendor_name: str | None
    payment_run_id: uuid.UUID
    payment_run_reference: str
    amount: str
    currency: str
    paid_at: datetime
    payment_reference: str | None
    days_since_paid: int


class PayReconciliationOut(BaseModel):
    generated_at: datetime
    days: int
    statements: list[PayStatementOut]
    unmatched: list[PayStatementLineOut]
    unmatched_count: int
    matched: list[PayStatementLineOut]
    matched_count: int
    ignored_count: int
    unexplained: list[PayUnexplainedOut]
    unexplained_count: int
    can_upload: bool


class PayStatementImportOut(BaseModel):
    statement: PayStatementOut
    matched: int
    unmatched: int


class PayMatchNowOut(BaseModel):
    matched: int
    unmatched: int


def _statement(s: PayBankStatement) -> PayStatementOut:
    return PayStatementOut(
        id=s.id,
        reference=s.reference,
        format=s.format,
        file_sha256=s.file_sha256,
        bank_statement_id=s.bank_statement_id,
        account_masked=s.account_masked,
        from_date=s.from_date,
        to_date=s.to_date,
        line_count=s.line_count,
        created_at=s.created_at,
    )


async def _lines(session: OrgSession, status: StatementLineStatus, limit: int) -> list[Any]:
    newest_first = status is StatementLineStatus.MATCHED
    order = (
        (PayBankStatementLine.matched_at.desc(), PayBankStatementLine.id)
        if newest_first
        else (PayBankStatementLine.booking_date, PayBankStatementLine.line_no)
    )
    rows = await session.execute(
        select(
            PayBankStatementLine,
            PayBankStatement.reference,
            PayPaymentRun.reference,
            PayInvoice.reference,
        )
        .join(PayBankStatement, PayBankStatement.id == PayBankStatementLine.statement_id)
        .outerjoin(PayPaymentRun, PayPaymentRun.id == PayBankStatementLine.payment_run_id)
        .outerjoin(PayInvoice, PayInvoice.id == PayBankStatementLine.invoice_id)
        .where(PayBankStatementLine.status == status.value)
        .order_by(*order)
        .limit(limit)
    )
    return list(rows.all())


def _line(row: Any) -> PayStatementLineOut:
    line, statement_ref, run_ref, invoice_ref = row
    return PayStatementLineOut(
        id=line.id,
        statement_id=line.statement_id,
        statement_reference=statement_ref,
        line_no=line.line_no,
        booking_date=line.booking_date,
        amount=amount_text(line.amount, line.currency) or "0",
        currency=line.currency,
        reference=line.reference,
        counterparty_iban_masked=line.counterparty_iban_masked,
        status=line.status,
        match_kind=line.match_kind,
        unmatched_reason=line.unmatched_reason,
        match_reasons=[str(r) for r in line.match_reasons],
        payment_run_id=line.payment_run_id,
        payment_run_reference=run_ref,
        invoice_id=line.invoice_id,
        invoice_reference=invoice_ref,
    )


async def _count(session: OrgSession, status: StatementLineStatus) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PayBankStatementLine)
            .where(PayBankStatementLine.status == status.value)
        )
        or 0
    )


@router.get("/reconciliation", response_model=PayReconciliationOut)
async def reconciliation(
    actor: CurrentActor,
    session: OrgSession,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> PayReconciliationOut:
    authorize(actor, RECONCILIATION_READ)
    now = datetime.now(UTC)
    statements = await session.scalars(
        select(PayBankStatement)
        .order_by(PayBankStatement.created_at.desc(), PayBankStatement.id)
        .limit(RECENT_STATEMENTS)
    )
    overdue = await overdue_items(session, now, days)
    return PayReconciliationOut(
        generated_at=now,
        days=days,
        statements=[_statement(s) for s in statements],
        unmatched=[
            _line(r) for r in await _lines(session, StatementLineStatus.UNMATCHED, MAX_LISTED)
        ],
        unmatched_count=await _count(session, StatementLineStatus.UNMATCHED),
        matched=[
            _line(r) for r in await _lines(session, StatementLineStatus.MATCHED, RECENT_MATCHES)
        ],
        matched_count=await _count(session, StatementLineStatus.MATCHED),
        ignored_count=await _count(session, StatementLineStatus.IGNORED),
        unexplained=[
            PayUnexplainedOut(
                invoice_id=o.paid.item.invoice_id,
                invoice_reference=o.paid.invoice_reference,
                invoice_number=o.paid.invoice_number,
                vendor_name=o.paid.vendor_name,
                payment_run_id=o.paid.item.payment_run_id,
                payment_run_reference=o.paid.run_reference,
                amount=amount_text(o.paid.item.amount, o.paid.item.currency) or "0",
                currency=o.paid.item.currency,
                paid_at=o.paid.item.paid_at or now,
                payment_reference=o.paid.item.payment_reference,
                days_since_paid=o.days_since_paid,
            )
            for o in overdue[:MAX_LISTED]
        ],
        unexplained_count=len(overdue),
        can_upload=actor.has(RECONCILIATION_UPLOAD),
    )


@router.post("/reconciliation/statements", response_model=PayStatementImportOut, status_code=201)
async def upload_statement(
    request: Request, body: PayStatementIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, RECONCILIATION_UPLOAD)
    person(actor)
    raw = body.content.encode("utf-8")
    try:
        parsed = parse_statement(body.format, raw)
    except InvalidStatementError as exc:
        return problem_response(
            request, 400, exc.code, detail=str(exc), errors=[e.as_json() for e in exc.errors]
        )
    statement, counts = await import_statement(session, actor, actor.org_id, parsed, raw)
    return PayStatementImportOut(
        statement=_statement(statement),
        matched=counts["matched"],
        unmatched=counts["unmatched"],
    )


@router.post("/reconciliation/match", response_model=PayMatchNowOut)
async def match_now(actor: CurrentActor, session: OrgSession) -> PayMatchNowOut:
    authorize(actor, RECONCILIATION_UPLOAD)
    person(actor)
    counts = await reconcile(session, actor)
    return PayMatchNowOut(matched=counts["matched"], unmatched=counts["unmatched"])

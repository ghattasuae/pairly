# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bank reconciliation: statement lines against what Pay recorded as paid.

A person imports a bank statement (``pairly_pay.statements``: CSV or camt.053). Each
booked debit is matched with a paid payment run item (one invoice), never guessed:

* the amount and currency must agree exactly (at the currency's minor unit), and
* the reference or the counterparty IBAN must agree as well: the line's reference
  contains the item's end-to-end id, the bank reference recorded when it was marked
  paid, the invoice number or the invoice's Pay reference (compared without case,
  spaces or separators, at least four characters); or the counterparty IBAN is the
  vendor bank account the run paid (blind index, the IBAN is never compared in clear).

The candidate with the most agreement wins; two equally good candidates leave the line
unmatched (``ambiguous``) for a person. A debit no item explains may be a whole run paid
as one batch: its reference names the run and its amount is the run's paid total.
Credits (money in) are ``ignored``: they are not Pay's to explain.

Lines stay ``unmatched`` until something explains them: the ``pay_reconciliation`` job
tries again every hour (a payment may be marked paid after the statement arrived). The
report lists the unmatched lines and the paid invoices that no statement line explains
``days`` after they were marked paid. Every match is audited; nothing here changes an
invoice or a run.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.types import seal
from pairly_core.db import set_tenant
from pairly_core.errors import ConflictError
from pairly_core.jobs.registry import JobContext, JobSpec
from pairly_core.rbac import Actor
from pairly_pay.checks.settings import to_minor_unit
from pairly_pay.common import By, audit, new_reference
from pairly_pay.models import (
    PayBankStatement,
    PayBankStatementLine,
    PayInvoice,
    PayPaymentRun,
    PayPaymentRunItem,
    PayVendorBankAccount,
    RunItemStatus,
    StatementLineStatus,
    StatementMatchKind,
)
from pairly_pay.statements import StatementInput, masked

log = structlog.get_logger("pairly_pay.reconciliation")

STATEMENT_ENTITY: Final = "pay.bank_statement"
LINE_ENTITY: Final = "pay.bank_statement_line"
JOB_NAME: Final = "pay_reconciliation"
DEFAULT_DAYS: Final = 5
MIN_KEY: Final = 4
_KEY_DROP: Final = re.compile(r"[^0-9A-Z]")


class StatementAlreadyImportedError(ConflictError):
    code = "statement_already_imported"
    title = "This statement was already imported"


def ref_key(value: str | None) -> str:
    """Upper case letters and digits only (``inv 2026/0079`` is ``INV20260079``)."""
    return _KEY_DROP.sub("", (value or "").upper())


# ------------------------------------------------------------------ matching (pure)


@dataclass(frozen=True, slots=True)
class LineFact:
    id: uuid.UUID
    booking_date: date
    amount: Decimal  # signed; a payment out is negative
    currency: str
    reference: str | None
    iban_bidx: str | None


@dataclass(frozen=True, slots=True)
class ItemFact:
    id: uuid.UUID
    run_id: uuid.UUID
    run_reference: str
    invoice_id: uuid.UUID
    amount: Decimal
    currency: str
    keys: tuple[str, ...]  # end-to-end id, bank reference, invoice number and reference
    iban_bidx: str | None
    # How many items of the run are paid (a run paid as one batch needs them all).
    run_paid_count: int


@dataclass(frozen=True, slots=True)
class Match:
    line_id: uuid.UUID
    kind: StatementMatchKind
    run_id: uuid.UUID
    item_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    reasons: tuple[str, ...]


@dataclass(slots=True)
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    # line id -> "no_candidate" or "ambiguous"
    unmatched: dict[uuid.UUID, str] = field(default_factory=dict)


def _same_amount(a: Decimal, b: Decimal, currency: str) -> bool:
    return to_minor_unit(a, currency) == to_minor_unit(b, currency)


def _evidence(line: LineFact, line_key: str, item: ItemFact) -> tuple[str, ...]:
    reasons = ["amount"]
    if any(len(k) >= MIN_KEY and k in line_key for k in item.keys):
        reasons.append("reference")
    if line.iban_bidx and item.iban_bidx and line.iban_bidx == item.iban_bidx:
        reasons.append("iban")
    return tuple(reasons)


def match_lines(lines: Sequence[LineFact], items: Sequence[ItemFact]) -> MatchResult:
    """Match debit ``lines`` with unexplained paid ``items`` (see the module docstring).
    Each item, and each run, explains one line at most. Deterministic: lines in booking
    date order, ties by the order given."""
    result = MatchResult()
    pool: dict[uuid.UUID, ItemFact] = {i.id: i for i in items}
    ordered = sorted(enumerate(lines), key=lambda p: (p[1].booking_date, p[0]))
    for _, line in ordered:
        if line.amount >= 0:
            continue
        paid = -line.amount
        line_key = ref_key(line.reference)
        scored = [
            (reasons, item)
            for item in pool.values()
            if item.currency == line.currency and _same_amount(item.amount, paid, line.currency)
            for reasons in (_evidence(line, line_key, item),)
            if len(reasons) > 1
        ]
        if scored:
            best = max(len(r) for r, _ in scored)
            top = [(r, i) for r, i in scored if len(r) == best]
            if len(top) > 1:
                result.unmatched[line.id] = "ambiguous"
                continue
            reasons, item = top[0]
            del pool[item.id]
            result.matches.append(
                Match(
                    line.id,
                    StatementMatchKind.ITEM,
                    item.run_id,
                    item.id,
                    item.invoice_id,
                    reasons,
                )
            )
            continue
        run = _run_match(line, line_key, paid, pool)
        if run is None:
            result.unmatched[line.id] = "no_candidate"
            continue
        if isinstance(run, str):
            result.unmatched[line.id] = run
            continue
        for item_id in [i.id for i in pool.values() if i.run_id == run]:
            del pool[item_id]
        result.matches.append(
            Match(line.id, StatementMatchKind.RUN, run, None, None, ("amount", "reference"))
        )
    return result


def _run_match(
    line: LineFact, line_key: str, paid: Decimal, pool: Mapping[uuid.UUID, ItemFact]
) -> uuid.UUID | str | None:
    """A run paid as one batch: the line names the run, every paid item of it is still
    unexplained and their total is the line's amount."""
    runs: dict[uuid.UUID, list[ItemFact]] = defaultdict(list)
    for item in pool.values():
        runs[item.run_id].append(item)
    found = [
        run_id
        for run_id, members in runs.items()
        if len(ref_key(members[0].run_reference)) >= MIN_KEY
        and ref_key(members[0].run_reference) in line_key
        and all(m.currency == line.currency for m in members)
        and len(members) == members[0].run_paid_count
        and _same_amount(sum((m.amount for m in members), Decimal(0)), paid, line.currency)
    ]
    if len(found) > 1:
        return "ambiguous"
    return found[0] if found else None


# ------------------------------------------------------------------ database


async def _explained(session: AsyncSession) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Item ids matched by a line, and run ids matched as one batch."""
    rows = await session.execute(
        select(
            PayBankStatementLine.payment_run_item_id,
            PayBankStatementLine.payment_run_id,
            PayBankStatementLine.match_kind,
        ).where(PayBankStatementLine.status == StatementLineStatus.MATCHED.value)
    )
    items: set[uuid.UUID] = set()
    runs: set[uuid.UUID] = set()
    for item_id, run_id, kind in rows.all():
        if kind == StatementMatchKind.RUN.value and run_id is not None:
            runs.add(run_id)
        elif item_id is not None:
            items.add(item_id)
    return items, runs


@dataclass(frozen=True, slots=True)
class PaidItem:
    item: PayPaymentRunItem
    run_reference: str
    invoice_reference: str
    invoice_number: str | None
    vendor_name: str | None
    iban_bidx: str | None


async def paid_items(session: AsyncSession) -> list[PaidItem]:
    rows = await session.execute(
        select(
            PayPaymentRunItem,
            PayPaymentRun.reference,
            PayInvoice.reference,
            PayInvoice.invoice_number,
            PayInvoice.vendor_name,
            PayVendorBankAccount.iban_bidx,
        )
        .join(PayPaymentRun, PayPaymentRun.id == PayPaymentRunItem.payment_run_id)
        .join(PayInvoice, PayInvoice.id == PayPaymentRunItem.invoice_id)
        .outerjoin(
            PayVendorBankAccount, PayVendorBankAccount.id == PayPaymentRunItem.bank_account_id
        )
        .where(PayPaymentRunItem.status == RunItemStatus.PAID.value)
        .order_by(PayPaymentRunItem.paid_at, PayPaymentRunItem.id)
    )
    return [
        PaidItem(item, run_ref, inv_ref, number, vendor, bidx)
        for item, run_ref, inv_ref, number, vendor, bidx in rows.all()
    ]


def _item_fact(p: PaidItem, run_counts: Mapping[uuid.UUID, int]) -> ItemFact:
    item = p.item
    keys = tuple(
        k
        for k in (
            ref_key(item.end_to_end_id),
            ref_key(item.payment_reference),
            ref_key(p.invoice_number),
            ref_key(p.invoice_reference),
        )
        if k
    )
    return ItemFact(
        id=item.id,
        run_id=item.payment_run_id,
        run_reference=p.run_reference,
        invoice_id=item.invoice_id,
        amount=item.amount,
        currency=item.currency,
        keys=keys,
        iban_bidx=p.iban_bidx,
        run_paid_count=run_counts[item.payment_run_id],
    )


async def unexplained_items(session: AsyncSession) -> list[PaidItem]:
    """Paid items no matched statement line explains (alone or as part of a run)."""
    items, runs = await _explained(session)
    return [
        p
        for p in await paid_items(session)
        if p.item.id not in items and p.item.payment_run_id not in runs
    ]


async def reconcile(session: AsyncSession, by: By | Actor) -> dict[str, int]:
    """Match every unmatched debit line of the organisation; audit each match."""
    lines = list(
        await session.scalars(
            select(PayBankStatementLine)
            .where(PayBankStatementLine.status == StatementLineStatus.UNMATCHED.value)
            .order_by(PayBankStatementLine.booking_date, PayBankStatementLine.line_no)
            .with_for_update()
        )
    )
    if not lines:
        return {"matched": 0, "unmatched": 0}
    paid = await paid_items(session)
    run_counts: dict[uuid.UUID, int] = defaultdict(int)
    for p in paid:
        run_counts[p.item.payment_run_id] += 1
    explained_items, explained_runs = await _explained(session)
    facts = [
        _item_fact(p, run_counts)
        for p in paid
        if p.item.id not in explained_items and p.item.payment_run_id not in explained_runs
    ]
    result = match_lines(
        [
            LineFact(
                line.id,
                line.booking_date,
                line.amount,
                line.currency,
                line.reference,
                line.counterparty_iban_bidx,
            )
            for line in lines
        ],
        facts,
    )
    now = datetime.now(UTC)
    by_id = {line.id: line for line in lines}
    for match in result.matches:
        line = by_id[match.line_id]
        line.status = StatementLineStatus.MATCHED.value
        line.match_kind = match.kind.value
        line.payment_run_id = match.run_id
        line.payment_run_item_id = match.item_id
        line.invoice_id = match.invoice_id
        line.match_reasons = list(match.reasons)
        line.unmatched_reason = None
        line.matched_at = now
        line.updated_at = now
    for line_id, reason in result.unmatched.items():
        line = by_id[line_id]
        if line.unmatched_reason != reason:
            line.unmatched_reason = reason
            line.updated_at = now
    await session.flush()
    for match in result.matches:
        await audit(
            session,
            by,
            "pay.statement_line.matched",
            entity_type=LINE_ENTITY,
            entity_id=match.line_id,
            payload={
                "kind": match.kind.value,
                "payment_run_id": str(match.run_id),
                "invoice_id": str(match.invoice_id) if match.invoice_id else None,
                "reasons": list(match.reasons),
            },
        )
    return {"matched": len(result.matches), "unmatched": len(result.unmatched)}


async def import_statement(
    session: AsyncSession,
    actor: Actor,
    org_id: uuid.UUID,
    parsed: StatementInput,
    raw: bytes,
) -> tuple[PayBankStatement, dict[str, int]]:
    """Store a parsed statement (refused when the same file was imported before),
    match its lines and audit the import with the file's digest."""
    digest = hashlib.sha256(raw).hexdigest()
    taken = await session.scalar(
        select(PayBankStatement.id).where(PayBankStatement.file_sha256 == digest)
    )
    if taken is not None:
        raise StatementAlreadyImportedError("this statement file was already imported")
    statement = PayBankStatement(
        id=uuid.uuid4(),
        org_id=org_id,
        reference=new_reference("STM"),
        format=parsed.format,
        file_sha256=digest,
        bank_statement_id=parsed.bank_statement_id,
        account_masked=parsed.account_masked,
        from_date=min(line.booking_date for line in parsed.lines),
        to_date=max(line.booking_date for line in parsed.lines),
        line_count=len(parsed.lines),
        imported_by=actor.id,
    )
    session.add(statement)
    await session.flush()
    for line in parsed.lines:
        line_id = uuid.uuid4()
        iban = line.counterparty_iban
        session.add(
            PayBankStatementLine(
                id=line_id,
                org_id=org_id,
                statement_id=statement.id,
                line_no=line.line_no,
                booking_date=line.booking_date,
                amount=line.amount,
                currency=line.currency,
                reference=line.reference,
                counterparty_iban=(
                    seal(
                        PayBankStatementLine,
                        "counterparty_iban",
                        iban,
                        org_id=org_id,
                        row_id=line_id,
                    )
                    if iban
                    else None
                ),
                counterparty_iban_bidx=blind_index("iban", iban, org_id) if iban else None,
                counterparty_iban_masked=masked(iban),
                status=(
                    StatementLineStatus.UNMATCHED.value
                    if line.amount < 0
                    else StatementLineStatus.IGNORED.value
                ),
            )
        )
    await session.flush()
    counts = await reconcile(session, actor)
    await audit(
        session,
        actor,
        "pay.bank_statement.imported",
        entity_type=STATEMENT_ENTITY,
        entity_id=statement.id,
        payload={
            "reference": statement.reference,
            "format": statement.format,
            "sha256": digest,
            "lines": statement.line_count,
            "matched": counts["matched"],
        },
    )
    return statement, counts


@dataclass(frozen=True, slots=True)
class Overdue:
    """A paid item no statement line explains ``days`` after it was marked paid."""

    paid: PaidItem
    days_since_paid: int


async def overdue_items(session: AsyncSession, now: datetime, days: int) -> list[Overdue]:
    cutoff = now - timedelta(days=days)
    return [
        Overdue(p, (now - p.item.paid_at).days)
        for p in await unexplained_items(session)
        if p.item.paid_at is not None and p.item.paid_at <= cutoff
    ]


# ------------------------------------------------------------------ the job


async def _org_ids(ctx: JobContext) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM pay_reconciliation_org_ids() AS org_id")
        )
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def reconcile_org(ctx: JobContext, org_id: uuid.UUID) -> dict[str, int]:
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        return await reconcile(session, By.system())


async def run_reconciliation(ctx: JobContext) -> Mapping[str, Any]:
    totals = {"orgs": 0, "matched": 0, "unmatched": 0, "errors": 0}
    for org_id in await _org_ids(ctx):
        totals["orgs"] += 1
        try:
            result = await reconcile_org(ctx, org_id)
        except Exception as exc:
            totals["errors"] += 1
            log.warning("pay_reconciliation_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        totals["matched"] += result["matched"]
        totals["unmatched"] += result["unmatched"]
    return totals


RECONCILIATION_JOB: Final = JobSpec(
    JOB_NAME,
    "Match unmatched bank statement lines with payments marked paid since the import",
    "every hour",
    run=run_reconciliation,
    every_minutes=60,
)

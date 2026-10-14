# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Payment runs (WP4.7, brief 4.6.3). No bank connection, no money moves (rule 3).

1. **Propose** (``plan_run`` then ``propose_run``, ``pay.payment_run.create``): approved
   invoices in one currency, due on or before a date, oldest due first, until the cash
   limit. Left out, with the reason: held vendors, open bank-detail changes, stale or
   failed checks, bank details or checks or amount that changed since the approval,
   anything over the cash limit. Each item binds the bank account, the keyed digest of
   its details and the check digest the invoice was approved with. The invoices become
   ``scheduled``. The early-payment discount is a placeholder (``discount`` is empty).
2. **Approve**: the ``pay_payment_run`` run (``pairly_pay.payment_run_graph``) asks a
   finance controller (``pay.payment_run``). The proposer is a preparer and never
   approves. The final action ``approve_payment_run`` checks every item again.
3. **Export** (``export_run``, ``pay.payment_run.export``): CSV or pain.001.001.09.
   Checked again right before the file is built, fail closed: every invoice is still
   scheduled in this run, its vendor is active, not held and has no open bank-detail
   change, the vendor master's active account is the very account (same id, same keyed
   digest) proposed, approved and bound to the invoice's approval, and the checks are
   current and unchanged since the approval. Any problem: no file, a 409 with every
   problem, and an audit event. The file's details come from the vendor master at that
   moment; only its SHA-256 is kept.
4. **Mark paid** (``mark_paid``, ``pay.payment.record``): a person records that the
   bank paid, with the bank's reference. The run is ``paid`` when every item is.

Rejection, a change request, a refused final action or a cancellation release the
invoices (back to ``approved``). Every step writes a decision record and an audit event.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.models import ApprovalDecision
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.rbac import Actor
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.bank_files import Batch, Payment, to_csv, to_pain001
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import By, audit, decision_ids_of, new_reference, record_step
from pairly_pay.freshness import check_inputs_digest, checks_failed
from pairly_pay.invoices import INVOICE_ENTITY, refresh_decisions
from pairly_pay.models import (
    InvoiceStatus,
    PayInvoice,
    PaymentRunStatus,
    PayPaymentRun,
    PayPaymentRunItem,
    PayVendor,
    RunItemStatus,
)
from pairly_pay.payability import blockers, same_details
from pairly_pay.payment_settings import load_payment_settings
from pairly_pay.vendors import account_name_of, active_account, iban_of

RUN_ENTITY: Final = "pay.payment_run"
REFERENCE_PREFIX: Final = "PR"
GRAPH: Final = "pay_payment_run"
VERSION: Final = 1
STEP_KEY: Final = "run_approval"
APPROVAL_TYPE: Final = "pay.payment_run"
FINAL_ACTION: Final = "approve_payment_run"
MAX_ITEMS: Final = 500
MAX_CANDIDATES: Final = 2000
PAYMENT_RUN_RULES_V1: Final = (
    files("pairly_pay")
    .joinpath("policy", "payment_run_approval_rules.yaml")
    .read_text(encoding="utf-8")
)
ExportFormat = Literal["csv", "pain001"]


class PaymentRunNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("payment run not found")


class PaymentRunStateError(ConflictError):
    pass


class NothingToPayError(ConflictError):
    code = "nothing_to_pay"
    title = "Nothing to pay"

    def __init__(self, excluded: Sequence[Mapping[str, Any]]) -> None:
        super().__init__("no approved invoice can go in this run")
        self.excluded = list(excluded)


class ExportRefusedError(ConflictError):
    """Something changed since the approval: no file (fail closed)."""

    code = "export_refused"
    title = "Bank file refused"

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("the bank file was not made: " + ", ".join(problems[:10]))
        self.problems = list(problems)


def money(value: Decimal | None, currency: str | None) -> str | None:
    """The amount at the currency's minor-unit precision (``amount_text``)."""
    return amount_text(value, currency)


# ------------------------------------------------------------------ reading


async def get_run(session: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> PayPaymentRun:
    query = select(PayPaymentRun).where(PayPaymentRun.id == run_id)
    if lock:
        query = query.with_for_update()
    run = await session.scalar(query.execution_options(populate_existing=True))
    if run is None:
        raise PaymentRunNotFoundError()
    return run


async def items_of(
    session: AsyncSession, run_id: uuid.UUID, *, active_only: bool = False
) -> list[PayPaymentRunItem]:
    query = select(PayPaymentRunItem).where(PayPaymentRunItem.payment_run_id == run_id)
    if active_only:
        query = query.where(PayPaymentRunItem.status != RunItemStatus.RELEASED.value)
    return list(
        await session.scalars(
            query.order_by(PayPaymentRunItem.position).execution_options(populate_existing=True)
        )
    )


async def refresh_run_decisions(session: AsyncSession, run: PayPaymentRun) -> None:
    run.decision_ids = await decision_ids_of(session, RUN_ENTITY, run.id)


# ------------------------------------------------------------------ the one check


async def invoice_problems(session: AsyncSession, invoice: PayInvoice) -> list[str]:
    """Why an approved invoice cannot be paid as approved now (empty: it can)."""
    out = list(await blockers(session, invoice))
    if invoice.vendor_id is None or invoice.approved_account_id is None:
        return [*out, "not_approved"]
    account = await active_account(session, invoice.vendor_id)
    if (
        account is None
        or account.id != invoice.approved_account_id
        or not same_details(invoice.approved_bank_digest, account)
    ):
        out.append("bank_details_changed")
    if invoice.approved_checks_digest is None or (
        await check_inputs_digest(session, invoice) != invoice.approved_checks_digest
    ):
        out.append("checks_changed_since_approval")
    if checks_failed(invoice):
        out.append("checks_failed")
    if invoice.total is None or invoice.total != invoice.approved_amount:
        out.append("amount_changed")
    if invoice.currency != invoice.approved_currency:
        out.append("currency_changed")
    return list(dict.fromkeys(out))


async def item_problems(
    session: AsyncSession, run: PayPaymentRun, item: PayPaymentRunItem
) -> list[str]:
    """``invoice_problems`` plus: the invoice is still this run's, with the bank account,
    details digest and amount the item was proposed (and approved) with."""
    invoice = await session.scalar(
        select(PayInvoice)
        .where(PayInvoice.id == item.invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invoice is None:
        return ["invoice_missing"]
    out: list[str] = []
    if invoice.status != InvoiceStatus.SCHEDULED.value or invoice.payment_run_id != run.id:
        out.append("invoice_not_in_this_run")
    out += await invoice_problems(session, invoice)
    if invoice.approved_account_id != item.bank_account_id:
        out.append("bank_details_changed")
    account = await active_account(session, item.vendor_id)
    if (
        account is None
        or account.id != item.bank_account_id
        or not same_details(item.bank_details_digest, account)
    ):
        out.append("bank_details_changed")
    if invoice.approved_checks_digest != item.checks_digest:
        out.append("checks_changed_since_approval")
    if invoice.approved_amount != item.amount:
        out.append("amount_changed")
    return list(dict.fromkeys(out))


def items_digest(items: Sequence[PayPaymentRunItem]) -> str:
    """What the finance controller approves: every item's invoice, amount, account and
    digests, in order."""
    material = [
        [
            str(i.invoice_id),
            money(i.amount, i.currency),
            i.currency,
            str(i.bank_account_id),
            i.bank_details_digest,
            i.checks_digest,
            i.end_to_end_id,
        ]
        for i in items
    ]
    raw = json.dumps(material, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------------ proposing


@dataclass(frozen=True, slots=True)
class PlannedItem:
    invoice: PayInvoice
    amount: Decimal


@dataclass(slots=True)
class Plan:
    currency: str
    cash_limit: Decimal
    execution_date: dt.date
    due_on_or_before: dt.date | None
    items: list[PlannedItem] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((i.amount for i in self.items), Decimal(0))


async def plan_run(
    session: AsyncSession,
    *,
    currency: str,
    cash_limit: Decimal,
    execution_date: dt.date,
    due_on_or_before: dt.date | None,
    invoice_ids: Sequence[uuid.UUID] | None = None,
    lock: bool = False,
) -> Plan:
    """Which approved invoices would go in a run, and why the others would not."""
    if cash_limit <= 0:
        raise BadRequestError("the cash limit must be more than zero")
    plan = Plan(currency, cash_limit, execution_date, due_on_or_before)
    query = select(PayInvoice).where(
        PayInvoice.status == InvoiceStatus.APPROVED.value,
        PayInvoice.payment_run_id.is_(None),
    )
    if invoice_ids is not None:
        query = query.where(PayInvoice.id.in_(list(invoice_ids)[:MAX_ITEMS]))
    if lock:
        query = query.with_for_update()
    rows = list(
        await session.scalars(
            query.order_by(
                PayInvoice.due_date.asc().nulls_last(), PayInvoice.created_at, PayInvoice.id
            )
            .limit(MAX_CANDIDATES)
            .execution_options(populate_existing=True)
        )
    )
    running = Decimal(0)
    for invoice in rows:
        reasons: list[str] = []
        if invoice.currency != currency:
            continue  # another run's currency: not a reason, just not this run
        if (
            due_on_or_before is not None
            and invoice.due_date is not None
            and invoice.due_date > due_on_or_before
        ):
            reasons.append("not_due_yet")
        reasons += await invoice_problems(session, invoice)
        amount = invoice.approved_amount
        if not reasons and amount is not None and running + amount > cash_limit:
            reasons.append("over_cash_limit")
        if not reasons and len(plan.items) >= MAX_ITEMS:
            reasons.append("run_full")
        if reasons or amount is None:
            plan.excluded.append(
                {
                    "invoice_id": str(invoice.id),
                    "reference": invoice.reference,
                    "amount": money(invoice.total, invoice.currency),
                    "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
                    "reasons": reasons or ["not_approved"],
                }
            )
            continue
        running += amount
        plan.items.append(PlannedItem(invoice, amount))
    return plan


async def propose_run(
    session: AsyncSession,
    actor: Actor,
    *,
    currency: str,
    cash_limit: Decimal,
    execution_date: dt.date,
    due_on_or_before: dt.date | None,
    invoice_ids: Sequence[uuid.UUID] | None = None,
) -> PayPaymentRun:
    """Create the run and schedule its invoices. The caller commits, then starts the
    approval run (``pay_payment_run``)."""
    plan = await plan_run(
        session,
        currency=currency,
        cash_limit=cash_limit,
        execution_date=execution_date,
        due_on_or_before=due_on_or_before,
        invoice_ids=invoice_ids,
        lock=True,
    )
    if not plan.items:
        raise NothingToPayError(plan.excluded)
    now = datetime.now(UTC)
    run = PayPaymentRun(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        reference=new_reference(REFERENCE_PREFIX),
        status=PaymentRunStatus.AWAITING_APPROVAL.value,
        currency=currency,
        cash_limit=cash_limit,
        total=plan.total,
        item_count=len(plan.items),
        execution_date=execution_date,
        due_on_or_before=due_on_or_before,
        excluded=plan.excluded[:500],
        proposed_by=actor.id,
        approver_ids=[],
        exports=[],
        decision_ids=[],
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    await session.flush()
    for position, planned in enumerate(plan.items, start=1):
        invoice = planned.invoice
        assert invoice.vendor_id is not None  # noqa: S101 (checked by invoice_problems)
        assert invoice.approved_account_id is not None  # noqa: S101
        session.add(
            PayPaymentRunItem(
                id=uuid.uuid4(),
                org_id=actor.org_id,
                payment_run_id=run.id,
                invoice_id=invoice.id,
                vendor_id=invoice.vendor_id,
                position=position,
                status=RunItemStatus.INCLUDED.value,
                amount=planned.amount,
                currency=currency,
                due_date=invoice.due_date,
                discount=None,
                bank_account_id=invoice.approved_account_id,
                bank_details_digest=str(invoice.approved_bank_digest),
                checks_digest=str(invoice.approved_checks_digest),
                end_to_end_id=f"{run.reference}-{position:04d}",
                created_at=now,
                updated_at=now,
            )
        )
        invoice.status = InvoiceStatus.SCHEDULED.value
        invoice.payment_run_id = run.id
        invoice.updated_at = now
    await session.flush()
    by = By.of(actor)
    await record_step(
        session,
        by,
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        action="pay.payment_run.proposed",
        inputs={
            "currency": currency,
            "cash_limit": money(cash_limit, currency),
            "execution_date": execution_date.isoformat(),
            "due_on_or_before": due_on_or_before.isoformat() if due_on_or_before else None,
            "excluded": plan.excluded[:100],
        },
        output={
            "items": [str(p.invoice.id) for p in plan.items],
            "total": money(plan.total, currency),
            "status": run.status,
        },
    )
    await audit(
        session,
        actor,
        "pay.payment_run.proposed",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={
            "reference": run.reference,
            "items": len(plan.items),
            "excluded": len(plan.excluded),
            "total": money(plan.total, currency),
            "currency": currency,
        },
    )
    for planned in plan.items:
        await audit(
            session,
            actor,
            "pay.invoice.scheduled",
            entity_type=INVOICE_ENTITY,
            entity_id=planned.invoice.id,
            payload={"payment_run_id": str(run.id), "reference": run.reference},
        )
    await refresh_run_decisions(session, run)
    return run


# ------------------------------------------------------------------ approval


def approval_payload(run: PayPaymentRun, items: Sequence[PayPaymentRunItem]) -> dict[str, Any]:
    return {
        "payment_run_id": str(run.id),
        "reference": run.reference,
        "currency": run.currency,
        "total": money(run.total, run.currency),
        "cash_limit": money(run.cash_limit, run.currency),
        "items": str(len(items)),
        "execution_date": run.execution_date.isoformat(),
        "items_digest": items_digest(items),
        "proposed_by": str(run.proposed_by),
    }


async def approval_context(
    session: AsyncSession, run: PayPaymentRun, items: Sequence[PayPaymentRunItem]
) -> dict[str, Any]:
    vendors = {
        v.id: v
        for v in await session.scalars(
            select(PayVendor).where(PayVendor.id.in_({i.vendor_id for i in items}))
        )
    }
    invoices = {
        i.id: i
        for i in await session.scalars(
            select(PayInvoice).where(PayInvoice.id.in_({i.invoice_id for i in items}))
        )
    }
    return {
        "kind": RUN_ENTITY,
        "payment_run_path": f"/pay/payment-runs/{run.id}",
        "items": [
            {
                "invoice_id": str(i.invoice_id),
                "invoice_reference": invoices[i.invoice_id].reference
                if i.invoice_id in invoices
                else None,
                "vendor": vendors[i.vendor_id].legal_name if i.vendor_id in vendors else None,
                "amount": money(i.amount, i.currency),
                "due_date": i.due_date.isoformat() if i.due_date else None,
            }
            for i in items
        ][:200],
        "excluded": list(run.excluded)[:100],
    }


async def approve_payment_run(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action, after the executor re-checked the approval. Checks every item
    again and the segregation of duties (fail closed)."""
    try:
        run = await get_run(session, uuid.UUID(str(call.payload.get("payment_run_id"))), lock=True)
    except (ValueError, PaymentRunNotFoundError):
        raise ExecutorRefusedError("payment_run_not_found") from None
    if run.approval_id != call.approval_id or run.run_id != call.run_id:
        raise ExecutorRefusedError("payment_run_not_of_this_approval")
    if run.status != PaymentRunStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("payment_run_not_awaiting_approval")
    items = await items_of(session, run.id, active_only=True)
    if call.payload.get("items_digest") != items_digest(items):
        raise ExecutorRefusedError("items_changed")
    for item in items:
        problems = await item_problems(session, run, item)
        if problems:
            raise ExecutorRefusedError(f"item_blocked:{problems[0]}")
    rows = (
        await session.execute(
            select(ApprovalDecision.actor_user_id, ApprovalDecision.on_behalf_of).where(
                ApprovalDecision.approval_id == call.approval_id
            )
        )
    ).all()
    involved = {r.actor_user_id for r in rows} | {r.on_behalf_of for r in rows if r.on_behalf_of}
    if run.proposed_by in involved | set(call.approver_ids):
        raise ExecutorRefusedError("proposer_approved")
    if not call.approver_ids:
        raise ExecutorRefusedError("no_human_approver")
    now = datetime.now(UTC)
    run.status = PaymentRunStatus.APPROVED.value
    run.approver_ids = [str(a) for a in call.approver_ids]
    run.approved_at = now
    run.updated_at = now
    await audit(
        session,
        By.system(),
        "pay.payment_run.approved",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={"approval_id": str(call.approval_id), "approvers": run.approver_ids},
    )
    return {"payment_run_id": str(run.id), "status": run.status}


async def release_run(
    session: AsyncSession,
    run: PayPaymentRun,
    status: PaymentRunStatus,
    reason: str,
    by: By,
) -> None:
    """End a run that will not be paid: its unpaid invoices are approved again."""
    now = datetime.now(UTC)
    for item in await items_of(session, run.id, active_only=True):
        if item.status != RunItemStatus.INCLUDED.value:
            continue
        item.status = RunItemStatus.RELEASED.value
        item.updated_at = now
        invoice = await session.scalar(
            select(PayInvoice).where(PayInvoice.id == item.invoice_id).with_for_update()
        )
        if (
            invoice is not None
            and invoice.payment_run_id == run.id
            and invoice.status == InvoiceStatus.SCHEDULED.value
        ):
            invoice.status = InvoiceStatus.APPROVED.value
            invoice.payment_run_id = None
            invoice.updated_at = now
            await audit(
                session,
                by,
                "pay.invoice.unscheduled",
                entity_type=INVOICE_ENTITY,
                entity_id=invoice.id,
                payload={"payment_run_id": str(run.id), "reason": reason[:120]},
            )
    run.status = status.value
    run.closed_at = now
    run.close_reason = reason[:120]
    run.updated_at = now
    await session.flush()
    await audit(
        session,
        by,
        f"pay.payment_run.{status.value}",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={"reason": reason[:120]},
    )
    await refresh_run_decisions(session, run)


async def close_after_decision(
    session: AsyncSession, run_id: uuid.UUID, approval_id: uuid.UUID, outcome: str
) -> None:
    run = await get_run(session, run_id, lock=True)
    if run.status != PaymentRunStatus.AWAITING_APPROVAL.value or run.approval_id != approval_id:
        return
    await release_run(
        session,
        run,
        PaymentRunStatus.REJECTED,
        "rejected_by_approver" if outcome == "rejected" else "changes_requested_by_approver",
        By.system(),
    )


async def fail_run(session: AsyncSession, run_id: uuid.UUID, reason: str) -> None:
    run = await get_run(session, run_id, lock=True)
    if run.status != PaymentRunStatus.AWAITING_APPROVAL.value:
        return
    await release_run(session, run, PaymentRunStatus.FAILED, reason, By.system())


async def cancel_run(
    session: AsyncSession, actor: Actor, run_id: uuid.UUID, reason: str
) -> PayPaymentRun:
    """A person cancels an approved or exported run before anything in it was paid."""
    run = await get_run(session, run_id, lock=True)
    if run.status not in {PaymentRunStatus.APPROVED.value, PaymentRunStatus.EXPORTED.value}:
        raise PaymentRunStateError(f"the payment run is {run.status}")
    items = await items_of(session, run.id, active_only=True)
    if any(i.status == RunItemStatus.PAID.value for i in items):
        raise PaymentRunStateError("part of this run is already marked paid")
    await record_step(
        session,
        By.of(actor),
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        action="pay.payment_run.cancelled",
        inputs={"previous_status": run.status},
        output={"status": PaymentRunStatus.CANCELLED.value},
        reason=reason,
    )
    await release_run(session, run, PaymentRunStatus.CANCELLED, "cancelled", By.of(actor))
    return run


# ------------------------------------------------------------------ export


@dataclass(frozen=True, slots=True)
class ExportFile:
    filename: str
    media_type: str
    content: bytes
    sha256: str


async def _audit_refusal(
    session: AsyncSession, actor: Actor, run: PayPaymentRun, fmt: str, problems: Sequence[str]
) -> None:
    await audit(
        session,
        actor,
        "pay.payment_run.export_refused",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={"format": fmt, "problems": list(problems)[:50]},
    )


async def check_export(
    session: AsyncSession, run: PayPaymentRun
) -> tuple[list[PayPaymentRunItem], list[str]]:
    """The items to pay and every problem found (``<end_to_end_id>:<problem>``)."""
    items = [
        i
        for i in await items_of(session, run.id, active_only=True)
        if i.status == RunItemStatus.INCLUDED.value
    ]
    problems: list[str] = []
    if not items:
        problems.append("nothing_left_to_pay")
    for item in items:
        problems += [f"{item.end_to_end_id}:{p}" for p in await item_problems(session, run, item)]
    return items, problems


async def export_run(
    session: AsyncSession, actor: Actor, run_id: uuid.UUID, fmt: ExportFormat
) -> ExportFile:
    """The bank file, after checking everything again. On any problem the refusal is
    audited and ``ExportRefusedError`` raised: the caller commits the audit event and
    answers 409. No file is ever built from details that changed."""
    run = await get_run(session, run_id, lock=True)
    if run.status not in {PaymentRunStatus.APPROVED.value, PaymentRunStatus.EXPORTED.value}:
        raise PaymentRunStateError(f"the payment run is {run.status}")
    items, problems = await check_export(session, run)
    settings = await load_payment_settings(session)
    if fmt == "pain001" and (not settings.payer_name or not settings.payer_iban):
        problems.append("payer_details_missing")
    if problems:
        await _audit_refusal(session, actor, run, fmt, problems)
        raise ExportRefusedError(problems)

    payments: list[Payment] = []
    for item in items:
        vendor = await session.scalar(select(PayVendor).where(PayVendor.id == item.vendor_id))
        account = await active_account(session, item.vendor_id)
        invoice = await session.scalar(select(PayInvoice).where(PayInvoice.id == item.invoice_id))
        if vendor is None or account is None or invoice is None:  # checked just above
            raise ExportRefusedError([f"{item.end_to_end_id}:changed"])
        payments.append(
            Payment(
                end_to_end_id=item.end_to_end_id,
                invoice_reference=invoice.reference,
                invoice_number=invoice.invoice_number,
                vendor_reference=vendor.reference,
                beneficiary_name=account_name_of(account),
                iban=iban_of(account),
                bic=account.swift,
                bank_name=account.bank_name,
                amount=item.amount,
                currency=item.currency,
            )
        )
    now = datetime.now(UTC)
    count = run.export_count + 1
    batch = Batch(
        reference=run.reference,
        message_id=f"{run.reference}-{count}",
        created_at=now,
        execution_date=run.execution_date,
        currency=run.currency,
        payer_name=settings.payer_name,
        payer_iban=settings.payer_iban,
        payer_bic=settings.payer_bic,
        payments=payments,
    )
    if fmt == "csv":
        content, media, ext = to_csv(batch), "text/csv; charset=utf-8", "csv"
    else:
        content, media, ext = to_pain001(batch), "application/xml", "xml"
    digest = hashlib.sha256(content).hexdigest()
    run.export_count = count
    run.exports = [
        *run.exports,
        {
            "format": fmt,
            "sha256": digest,
            "items": len(payments),
            "at": now.isoformat(),
            "by": str(actor.id),
        },
    ][-50:]
    run.exported_at = now
    if run.status == PaymentRunStatus.APPROVED.value:
        run.status = PaymentRunStatus.EXPORTED.value
    run.updated_at = now
    await record_step(
        session,
        By.of(actor),
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        action="pay.payment_run.exported",
        inputs={"format": fmt, "items": [str(i.invoice_id) for i in items]},
        output={"sha256": digest, "status": run.status, "export": count},
    )
    await audit(
        session,
        actor,
        "pay.payment_run.exported",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={"format": fmt, "sha256": digest, "items": len(payments)},
    )
    await refresh_run_decisions(session, run)
    return ExportFile(f"{run.reference}-{count}.{ext}", media, content, digest)


# ------------------------------------------------------------------ paid


async def mark_paid(
    session: AsyncSession,
    actor: Actor,
    run_id: uuid.UUID,
    *,
    invoice_ids: Sequence[uuid.UUID] | None,
    reference: str,
    note: str | None,
) -> PayPaymentRun:
    """A person records that the bank paid (all items, or the ones named)."""
    run = await get_run(session, run_id, lock=True)
    if run.status != PaymentRunStatus.EXPORTED.value:
        raise PaymentRunStateError("export the bank file first")
    items = await items_of(session, run.id, active_only=True)
    wanted = set(invoice_ids) if invoice_ids is not None else None
    chosen = [
        i
        for i in items
        if i.status == RunItemStatus.INCLUDED.value and (wanted is None or i.invoice_id in wanted)
    ]
    if wanted is not None and len(chosen) != len(wanted):
        raise PaymentRunStateError("some invoices are not unpaid items of this run")
    if not chosen:
        raise PaymentRunStateError("nothing left to mark paid")
    now = datetime.now(UTC)
    by = By.of(actor)
    for item in chosen:
        invoice = await session.scalar(
            select(PayInvoice).where(PayInvoice.id == item.invoice_id).with_for_update()
        )
        if (
            invoice is None
            or invoice.status != InvoiceStatus.SCHEDULED.value
            or invoice.payment_run_id != run.id
        ):
            raise PaymentRunStateError("an invoice of this run is not scheduled any more")
        item.status = RunItemStatus.PAID.value
        item.paid_at = now
        item.paid_by = actor.id
        item.payment_reference = reference
        item.updated_at = now
        invoice.status = InvoiceStatus.PAID.value
        invoice.paid_at = now
        invoice.paid_by = actor.id
        invoice.payment_reference = reference
        invoice.updated_at = now
        await record_step(
            session,
            by,
            entity_type=INVOICE_ENTITY,
            entity_id=invoice.id,
            action="pay.invoice.paid",
            inputs={"payment_run_id": str(run.id), "end_to_end_id": item.end_to_end_id},
            output={"status": invoice.status, "payment_reference": reference},
            reason=note,
        )
        await audit(
            session,
            actor,
            "pay.invoice.paid",
            entity_type=INVOICE_ENTITY,
            entity_id=invoice.id,
            payload={"payment_run_id": str(run.id), "payment_reference": reference},
        )
        await refresh_decisions(session, invoice)
    await session.flush()
    remaining = [
        i for i in await items_of(session, run.id, active_only=True)
        if i.status == RunItemStatus.INCLUDED.value
    ]  # fmt: skip
    if not remaining:
        run.status = PaymentRunStatus.PAID.value
        run.paid_at = now
    run.updated_at = now
    await record_step(
        session,
        by,
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        action="pay.payment_run.marked_paid",
        inputs={"invoices": [str(i.invoice_id) for i in chosen]},
        output={"status": run.status, "payment_reference": reference},
        reason=note,
    )
    await audit(
        session,
        actor,
        "pay.payment_run.marked_paid",
        entity_type=RUN_ENTITY,
        entity_id=run.id,
        payload={"invoices": len(chosen), "status": run.status},
    )
    await refresh_run_decisions(session, run)
    return run

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Payment run routes (WP4.7, ``pairly_pay.payment_runs``). No bank connection.

* ``GET  /payment-runs``                      newest first (``pay.payment_run.read``).
* ``GET  /payment-runs/{id}``                 one run with its items and what was left out.
* ``POST /payment-runs/preview``              what a proposal would include and leave out,
  and why (``pay.payment_run.create``). Writes nothing.
* ``POST /payment-runs``                      propose a run (``pay.payment_run.create``,
  people only). The approval run starts after the commit; the proposer never approves.
* ``POST /payment-runs/{id}/export``          download the bank file, ``format`` ``csv`` or
  ``pain001`` (``pay.payment_run.export``, a second factor). Everything is checked again
  first; a change since the approval answers 409 with every problem, and no file.
* ``POST /payment-runs/{id}/paid``            mark invoices paid after the bank confirmed
  (``pay.payment.record``).
* ``POST /payment-runs/{id}/cancel``          cancel an approved or exported run before
  anything was paid (``pay.payment_run.create``); its invoices are approved again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from pairly_api.problems import problem_response
from pairly_api.routes.approvals import get_runtime
from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.common import WORKFLOW
from pairly_pay.models import PayInvoice, PayPaymentRun, PayVendor
from pairly_pay.payment_runs import (
    GRAPH,
    RUN_ENTITY,
    ExportRefusedError,
    NothingToPayError,
    Plan,
    cancel_run,
    export_run,
    fail_run,
    get_run,
    items_of,
    mark_paid,
    money,
    plan_run,
    propose_run,
)
from pairly_pay.payment_settings import load_payment_settings
from pairly_pay.permissions import (
    PAYMENT_RECORD,
    PAYMENT_RUN_APPROVE,
    PAYMENT_RUN_CREATE,
    PAYMENT_RUN_EXPORT,
    PAYMENT_RUN_READ,
)
from pairly_pay.vendors import active_account, masked_account

log = structlog.get_logger("pairly_pay.api.payment_runs")

router = APIRouter(tags=["pay"])
Runtime = Annotated[WorkflowRuntime, Depends(get_runtime)]
MAX_LIST: Final = 200
RUN_STATUS = Literal[
    "awaiting_approval", "approved", "exported", "paid", "rejected", "cancelled", "failed"
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayRunProposalIn(_In):
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    cash_limit: Annotated[Decimal, Field(gt=0, le=1_000_000_000_000)] | None = None
    execution_date: dt.date
    due_on_or_before: dt.date | None = None
    invoice_ids: Annotated[list[uuid.UUID], Field(max_length=500)] | None = None


class PayRunExportIn(_In):
    format: Literal["csv", "pain001"] = "csv"


class PayRunPaidIn(_In):
    invoice_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=500)] | None = None
    reference: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w./:#-]+$")]
    note: str | None = Field(default=None, max_length=1000)


class PayRunCancelIn(_In):
    reason: str = Field(min_length=1, max_length=1000)


class PayRunItemOut(BaseModel):
    id: uuid.UUID
    position: int
    status: str
    invoice_id: uuid.UUID
    invoice_reference: str | None
    invoice_number: str | None
    vendor_id: uuid.UUID
    vendor_name: str | None
    amount: Decimal
    currency: str
    due_date: dt.date | None
    # Placeholder for the early-payment discount optimiser: always empty for now.
    discount: Decimal | None
    bank_account_masked: str | None
    end_to_end_id: str
    paid_at: dt.datetime | None
    payment_reference: str | None


class PayRunExcludedOut(BaseModel):
    invoice_id: uuid.UUID
    reference: str | None
    amount: str | None
    due_date: str | None
    reasons: list[str]


class PayRunSummary(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    currency: str
    cash_limit: Decimal
    total: Decimal
    item_count: int
    execution_date: dt.date
    proposed_by: uuid.UUID
    approval_id: uuid.UUID | None
    approved_at: dt.datetime | None
    exported_at: dt.datetime | None
    paid_at: dt.datetime | None
    created_at: dt.datetime


class PayRunDetail(PayRunSummary):
    due_on_or_before: dt.date | None
    approver_ids: list[str]
    items: list[PayRunItemOut]
    excluded: list[PayRunExcludedOut]
    exports: list[dict[str, Any]]
    close_reason: str | None
    decision_ids: list[str]
    can_approve: bool
    can_export: bool
    can_mark_paid: bool
    can_cancel: bool


class PayRunList(BaseModel):
    items: list[PayRunSummary]
    counts: dict[str, int]


class PayRunPreview(BaseModel):
    currency: str
    cash_limit: Decimal
    total: Decimal
    items: list[dict[str, Any]]
    excluded: list[PayRunExcludedOut]


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


def _summary(run: PayPaymentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "reference": run.reference,
        "status": run.status,
        "currency": run.currency,
        "cash_limit": money(run.cash_limit, run.currency),
        "total": money(run.total, run.currency),
        "item_count": run.item_count,
        "execution_date": run.execution_date,
        "proposed_by": run.proposed_by,
        "approval_id": run.approval_id,
        "approved_at": run.approved_at,
        "exported_at": run.exported_at,
        "paid_at": run.paid_at,
        "created_at": run.created_at,
    }


def _excluded(rows: list[Any]) -> list[PayRunExcludedOut]:
    return [
        PayRunExcludedOut(
            invoice_id=uuid.UUID(str(r["invoice_id"])),
            reference=r.get("reference"),
            amount=r.get("amount"),
            due_date=r.get("due_date"),
            reasons=list(r.get("reasons") or []),
        )
        for r in rows
        if isinstance(r, dict) and r.get("invoice_id")
    ]


async def _detail(session: OrgSession, actor: Actor, run: PayPaymentRun) -> PayRunDetail:
    items = await items_of(session, run.id)
    invoices = {
        i.id: i
        for i in await session.scalars(
            select(PayInvoice).where(PayInvoice.id.in_({i.invoice_id for i in items}))
        )
    }
    vendors = {
        v.id: v
        for v in await session.scalars(
            select(PayVendor).where(PayVendor.id.in_({i.vendor_id for i in items}))
        )
    }
    out_items: list[PayRunItemOut] = []
    for item in items:
        invoice = invoices.get(item.invoice_id)
        vendor = vendors.get(item.vendor_id)
        account = await active_account(session, item.vendor_id)
        masked = (
            masked_account(account) if account is not None and account.id == item.bank_account_id
            else None
        )  # fmt: skip
        out_items.append(
            PayRunItemOut(
                id=item.id,
                position=item.position,
                status=item.status,
                invoice_id=item.invoice_id,
                invoice_reference=invoice.reference if invoice else None,
                invoice_number=invoice.invoice_number if invoice else None,
                vendor_id=item.vendor_id,
                vendor_name=vendor.legal_name if vendor else None,
                amount=Decimal(money(item.amount, item.currency) or "0"),
                currency=item.currency,
                due_date=item.due_date,
                discount=item.discount,
                bank_account_masked=masked,
                end_to_end_id=item.end_to_end_id,
                paid_at=item.paid_at,
                payment_reference=item.payment_reference,
            )
        )
    open_run = run.status in {"approved", "exported"}
    return PayRunDetail(
        **_summary(run),
        due_on_or_before=run.due_on_or_before,
        approver_ids=list(run.approver_ids),
        items=out_items,
        excluded=_excluded(list(run.excluded)),
        exports=list(run.exports),
        close_reason=run.close_reason,
        decision_ids=list(run.decision_ids),
        can_approve=actor.has(PAYMENT_RUN_APPROVE)
        and run.status == "awaiting_approval"
        and actor.id != run.proposed_by,
        can_export=actor.has(PAYMENT_RUN_EXPORT) and open_run,
        can_mark_paid=actor.has(PAYMENT_RECORD) and run.status == "exported",
        can_cancel=actor.has(PAYMENT_RUN_CREATE) and open_run,
    )


def _preview(plan: Plan) -> PayRunPreview:
    return PayRunPreview(
        currency=plan.currency,
        cash_limit=Decimal(money(plan.cash_limit, plan.currency) or "0"),
        total=Decimal(money(plan.total, plan.currency) or "0"),
        items=[
            {
                "invoice_id": str(p.invoice.id),
                "reference": p.invoice.reference,
                "vendor_name": p.invoice.vendor_name,
                "amount": money(p.amount, plan.currency),
                "due_date": p.invoice.due_date.isoformat() if p.invoice.due_date else None,
                "discount": None,
            }
            for p in plan.items
        ],
        excluded=_excluded(plan.excluded),
    )


async def _cash_limit(session: OrgSession, body: PayRunProposalIn) -> Decimal | None:
    if body.cash_limit is not None:
        return body.cash_limit
    return (await load_payment_settings(session)).default_cash_limit


# ------------------------------------------------------------------ reading


@router.get("/payment-runs", response_model=PayRunList)
async def list_runs(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[RUN_STATUS | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> PayRunList:
    authorize(actor, PAYMENT_RUN_READ)
    query = select(PayPaymentRun)
    if status is not None:
        query = query.where(PayPaymentRun.status == status)
    rows = await session.scalars(
        query.order_by(PayPaymentRun.created_at.desc(), PayPaymentRun.id).limit(limit)
    )
    counts = await session.execute(
        select(PayPaymentRun.status, func.count()).group_by(PayPaymentRun.status)
    )
    return PayRunList(
        items=[PayRunSummary(**_summary(r)) for r in rows],
        counts={str(s): int(n) for s, n in counts.all()},
    )


@router.get("/payment-runs/{run_id}", response_model=PayRunDetail)
async def get_run_detail(
    run_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayRunDetail:
    authorize(actor, PAYMENT_RUN_READ)
    return await _detail(session, actor, await get_run(session, run_id))


# ------------------------------------------------------------------ proposing


@router.post("/payment-runs/preview", response_model=PayRunPreview)
async def preview_run(
    request: Request, body: PayRunProposalIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, PAYMENT_RUN_CREATE)
    limit = await _cash_limit(session, body)
    if limit is None:
        return problem_response(request, 400, "cash_limit_required", detail="set a cash limit")
    plan = await plan_run(
        session,
        currency=body.currency,
        cash_limit=limit,
        execution_date=body.execution_date,
        due_on_or_before=body.due_on_or_before,
        invoice_ids=body.invoice_ids,
    )
    return _preview(plan)


@router.post("/payment-runs", status_code=201, response_model=PayRunDetail)
async def propose(
    request: Request,
    body: PayRunProposalIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> Any:
    authorize(actor, PAYMENT_RUN_CREATE)
    _person(actor)
    limit = await _cash_limit(session, body)
    if limit is None:
        return problem_response(request, 400, "cash_limit_required", detail="set a cash limit")
    try:
        run = await propose_run(
            session,
            actor,
            currency=body.currency,
            cash_limit=limit,
            execution_date=body.execution_date,
            due_on_or_before=body.due_on_or_before,
            invoice_ids=body.invoice_ids,
        )
    except NothingToPayError as exc:
        return problem_response(
            request,
            409,
            "nothing_to_pay",
            detail=str(exc),
            excluded=[e.model_dump(mode="json") for e in _excluded(exc.excluded)],
        )
    run_id = run.id
    await session.commit()
    try:
        await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"payment_run_id": str(run_id)},
            entity_type=RUN_ENTITY,
            entity_id=str(run_id),
        )
    except Exception as exc:
        log.warning("pay_payment_run_not_started", error=type(exc).__name__)
    run = await get_run(session, run_id, lock=True)
    if run.status == "awaiting_approval" and run.approval_id is None:
        # The approval was never requested: release the invoices (fail closed).
        await fail_run(session, run_id, "approval_not_started")
    await session.commit()
    return await _detail(session, actor, await get_run(session, run_id))


# ------------------------------------------------------------------ export, paid, cancel


@router.post(
    "/payment-runs/{run_id}/export",
    responses={
        200: {"content": {"text/csv": {}, "application/xml": {}}},
        409: {"description": "Something changed since the approval: no file"},
    },
)
async def export(
    request: Request,
    run_id: uuid.UUID,
    body: PayRunExportIn,
    actor: CurrentActor,
    session: OrgSession,
) -> Any:
    authorize(actor, PAYMENT_RUN_EXPORT)
    _person(actor)
    try:
        file = await export_run(session, actor, run_id, body.format)
    except ExportRefusedError as exc:
        await session.commit()  # keep the audit event of the refusal
        return problem_response(
            request, 409, "export_refused", detail=str(exc), problems=exc.problems
        )
    await session.commit()
    return Response(
        file.content,
        media_type=file.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "X-Content-SHA256": file.sha256,
            "Cache-Control": "no-store",
        },
    )


@router.post("/payment-runs/{run_id}/paid", response_model=PayRunDetail)
async def paid(
    run_id: uuid.UUID, body: PayRunPaidIn, actor: CurrentActor, session: OrgSession
) -> PayRunDetail:
    authorize(actor, PAYMENT_RECORD)
    _person(actor)
    run = await mark_paid(
        session,
        actor,
        run_id,
        invoice_ids=body.invoice_ids,
        reference=body.reference,
        note=body.note,
    )
    return await _detail(session, actor, run)


@router.post("/payment-runs/{run_id}/cancel", response_model=PayRunDetail)
async def cancel(
    run_id: uuid.UUID, body: PayRunCancelIn, actor: CurrentActor, session: OrgSession
) -> PayRunDetail:
    authorize(actor, PAYMENT_RUN_CREATE)
    _person(actor)
    run = await cancel_run(session, actor, run_id, body.reason)
    return await _detail(session, actor, run)

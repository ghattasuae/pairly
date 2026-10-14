# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Purchase order and goods receipt routes (brief 4.5.1, ``pairly_pay.purchase_orders``).

* ``GET  /purchase-orders?vendor_id=&status=&q=&limit=``  newest first, with what was
  received and invoiced so far (``pay.purchase_order.read``).
* ``GET  /purchase-orders/{id}``          one PO: its lines with the quantity ordered,
  received and invoiced, its receipts and the invoices matched with it.
* ``POST /purchase-orders``               create one (``pay.purchase_order.write``).
* ``POST /purchase-orders/import``        ``{"csv": "..."}``, one row per line; all or
  nothing, every problem with its row (400 ``invalid_purchase_data``).
* ``POST /goods-receipts``                record one receipt against an open PO.
* ``POST /goods-receipts/import``         ``{"csv": "..."}``, one row per PO line.

Numbers already on file answer 409 ``number_taken``. Every creation is audited.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from pairly_api.problems import problem_response
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import By
from pairly_pay.models import (
    InvoiceStatus,
    MatchType,
    PayGoodsReceipt,
    PayInvoice,
    PayPurchaseOrder,
    PayVendor,
)
from pairly_pay.permissions import PURCHASE_ORDER_READ, PURCHASE_ORDER_WRITE
from pairly_pay.purchase_orders import (
    MAX_CSV_BYTES,
    InvalidPurchaseDataError,
    PoInput,
    PoLineInput,
    PurchaseNumberTakenError,
    ReceiptInput,
    ReceiptLineInput,
    allocation_of,
    create_goods_receipt,
    create_purchase_order,
    get_purchase_order,
    import_goods_receipts,
    import_purchase_orders,
    po_lines,
    receipts_of,
    received_quantities,
)

router = APIRouter(tags=["pay"])
MAX_LIST: Final = 200
NUMBER = r"^[A-Za-z0-9][A-Za-z0-9 ./#_-]{0,63}$"
Quantity = Annotated[Decimal, Field(gt=0, le=Decimal("1000000000000"), decimal_places=4)]
Price = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000000"), decimal_places=4)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoLineIn(_In):
    line_no: Annotated[int, Field(ge=1, le=100_000)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Quantity
    unit_price: Price


class PurchaseOrderIn(_In):
    po_number: Annotated[str, Field(pattern=NUMBER)]
    vendor_id: uuid.UUID
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    order_date: dt.date
    match_type: Literal["2-way", "3-way"] = "3-way"
    department: Annotated[str, Field(max_length=64)] | None = None
    requester: Annotated[str, Field(max_length=120)] | None = None
    lines: Annotated[list[PoLineIn], Field(min_length=1, max_length=500)]


class ReceiptLineIn(_In):
    po_line_no: Annotated[int, Field(ge=1, le=100_000)]
    quantity: Quantity


class GoodsReceiptIn(_In):
    grn_number: Annotated[str, Field(pattern=NUMBER)]
    po_id: uuid.UUID
    received_date: dt.date
    received_by: Annotated[str, Field(max_length=120)] | None = None
    lines: Annotated[list[ReceiptLineIn], Field(min_length=1, max_length=500)]


class CsvIn(_In):
    csv: Annotated[str, Field(min_length=1, max_length=MAX_CSV_BYTES)]


class PoSummary(BaseModel):
    id: uuid.UUID
    po_number: str
    vendor_id: uuid.UUID
    vendor_name: str | None
    vendor_reference: str | None
    status: str
    match_type: str
    currency: str
    order_date: dt.date
    department: str | None
    subtotal: str
    line_count: int
    receipt_count: int
    invoice_count: int
    source: str
    created_at: dt.datetime


class PoLineOut(BaseModel):
    line_no: int
    description: str
    quantity: str
    unit_price: str
    line_total: str
    received: str
    invoiced: str


class ReceiptOut(BaseModel):
    id: uuid.UUID
    grn_number: str
    po_id: uuid.UUID
    received_date: dt.date
    received_by: str | None
    line_count: int
    source: str
    created_at: dt.datetime


class PoInvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    invoice_number: str | None
    status: str
    total: str | None
    matching: str | None


class PoDetail(PoSummary):
    requester: str | None
    lines: list[PoLineOut]
    receipts: list[ReceiptOut]
    invoices: list[PoInvoiceOut]


class PoList(BaseModel):
    items: list[PoSummary]
    counts: dict[str, int]


class ImportOut(BaseModel):
    created: int
    ids: list[uuid.UUID]
    numbers: list[str]


def number_text(value: Decimal) -> str:
    """A quantity or price as a plain string without trailing zeros (``"3"``, ``"412.345"``)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _problem(request: Request, exc: InvalidPurchaseDataError | PurchaseNumberTakenError) -> Any:
    if isinstance(exc, PurchaseNumberTakenError):
        return problem_response(request, 409, exc.code, detail=str(exc), numbers=exc.numbers)
    return problem_response(
        request, 400, exc.code, detail=str(exc), errors=[e.as_json() for e in exc.errors]
    )


async def _counts(
    session: OrgSession, ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    if not ids:
        return {}, {}
    receipts = await session.execute(
        select(PayGoodsReceipt.po_id, func.count())
        .where(PayGoodsReceipt.po_id.in_(ids))
        .group_by(PayGoodsReceipt.po_id)
    )
    invoices = await session.execute(
        select(PayInvoice.po_id, func.count())
        .where(PayInvoice.po_id.in_(ids), PayInvoice.status != InvoiceStatus.REJECTED.value)
        .group_by(PayInvoice.po_id)
    )
    return (
        {p: int(n) for p, n in receipts.all()},
        {p: int(n) for p, n in invoices.all() if p is not None},
    )


def _summary(
    po: PayPurchaseOrder, vendor: PayVendor | None, receipts: int, invoices: int
) -> dict[str, Any]:
    return {
        "id": po.id,
        "po_number": po.po_number,
        "vendor_id": po.vendor_id,
        "vendor_name": vendor.legal_name if vendor else None,
        "vendor_reference": vendor.reference if vendor else None,
        "status": po.status,
        "match_type": po.match_type,
        "currency": po.currency,
        "order_date": po.order_date,
        "department": po.department,
        "subtotal": amount_text(po.subtotal, po.currency),
        "line_count": po.line_count,
        "receipt_count": receipts,
        "invoice_count": invoices,
        "source": po.source,
        "created_at": po.created_at,
    }


def _receipt(r: PayGoodsReceipt) -> ReceiptOut:
    return ReceiptOut(
        id=r.id,
        grn_number=r.grn_number,
        po_id=r.po_id,
        received_date=r.received_date,
        received_by=r.received_by,
        line_count=r.line_count,
        source=r.source,
        created_at=r.created_at,
    )


async def _detail(session: OrgSession, po: PayPurchaseOrder) -> PoDetail:
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == po.vendor_id))
    received = await received_quantities(session, po.id)
    invoices = list(
        await session.scalars(
            select(PayInvoice)
            .where(PayInvoice.po_id == po.id)
            .order_by(PayInvoice.created_at, PayInvoice.id)
            .limit(500)
        )
    )
    invoiced: dict[int, Decimal] = {}
    for invoice in invoices:
        if invoice.status == InvoiceStatus.REJECTED.value:
            continue
        for line_no, quantity in allocation_of(invoice).items():
            invoiced[line_no] = invoiced.get(line_no, Decimal(0)) + quantity
    receipts = await receipts_of(session, po.id)
    live = [i for i in invoices if i.status != InvoiceStatus.REJECTED.value]
    return PoDetail(
        **_summary(po, vendor, len(receipts), len(live)),
        requester=po.requester,
        lines=[
            PoLineOut(
                line_no=line.line_no,
                description=line.description,
                quantity=number_text(line.quantity),
                unit_price=number_text(line.unit_price),
                line_total=amount_text(line.line_total, po.currency) or "0",
                received=number_text(received.get(line.line_no, Decimal(0))),
                invoiced=number_text(invoiced.get(line.line_no, Decimal(0))),
            )
            for line in await po_lines(session, po.id)
        ],
        receipts=[_receipt(r) for r in receipts],
        invoices=[
            PoInvoiceOut(
                id=i.id,
                reference=i.reference,
                invoice_number=i.invoice_number,
                status=i.status,
                total=amount_text(i.total, i.currency),
                matching=str(i.po_match.get("status")) if i.po_match.get("status") else None,
            )
            for i in invoices
        ],
    )


# ------------------------------------------------------------------ reading


@router.get("/purchase-orders", response_model=PoList)
async def list_purchase_orders(
    actor: CurrentActor,
    session: OrgSession,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[Literal["open", "closed", "cancelled"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> PoList:
    authorize(actor, PURCHASE_ORDER_READ)
    query = select(PayPurchaseOrder)
    if vendor_id is not None:
        query = query.where(PayPurchaseOrder.vendor_id == vendor_id)
    if status is not None:
        query = query.where(PayPurchaseOrder.status == status)
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(
                PayPurchaseOrder.po_number.ilike(like, escape="\\"),
                PayPurchaseOrder.vendor_id.in_(
                    select(PayVendor.id).where(PayVendor.legal_name.ilike(like, escape="\\"))
                ),
            )
        )
    rows = list(
        await session.scalars(
            query.order_by(PayPurchaseOrder.created_at.desc(), PayPurchaseOrder.id).limit(limit)
        )
    )
    vendors = {
        v.id: v
        for v in await session.scalars(
            select(PayVendor).where(PayVendor.id.in_({r.vendor_id for r in rows}))
        )
    }
    receipts, invoices = await _counts(session, [r.id for r in rows])
    counts = await session.execute(
        select(PayPurchaseOrder.status, func.count()).group_by(PayPurchaseOrder.status)
    )
    return PoList(
        items=[
            PoSummary(
                **_summary(
                    r, vendors.get(r.vendor_id), receipts.get(r.id, 0), invoices.get(r.id, 0)
                )
            )
            for r in rows
        ],
        counts={str(s): int(n) for s, n in counts.all()},
    )


@router.get("/purchase-orders/{po_id}", response_model=PoDetail)
async def get_purchase_order_detail(
    po_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PoDetail:
    authorize(actor, PURCHASE_ORDER_READ)
    return await _detail(session, await get_purchase_order(session, po_id))


# ------------------------------------------------------------------ creating


@router.post("/purchase-orders", status_code=201, response_model=PoDetail)
async def create(
    request: Request, body: PurchaseOrderIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, PURCHASE_ORDER_WRITE)
    data = PoInput(
        po_number=body.po_number,
        vendor_id=body.vendor_id,
        currency=body.currency,
        order_date=body.order_date,
        match_type=MatchType(body.match_type),
        department=body.department,
        requester=body.requester,
        lines=tuple(
            PoLineInput(x.line_no, x.description, x.quantity, x.unit_price) for x in body.lines
        ),
    )
    try:
        po = await create_purchase_order(session, By.of(actor), data)
    except (InvalidPurchaseDataError, PurchaseNumberTakenError) as exc:
        return _problem(request, exc)
    return await _detail(session, po)


@router.post("/purchase-orders/import", response_model=ImportOut)
async def import_pos(
    request: Request, body: CsvIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, PURCHASE_ORDER_WRITE)
    try:
        result = await import_purchase_orders(session, By.of(actor), body.csv)
    except (InvalidPurchaseDataError, PurchaseNumberTakenError) as exc:
        return _problem(request, exc)
    return ImportOut(created=len(result.created), ids=result.created, numbers=result.numbers)


@router.get("/goods-receipts", response_model=list[ReceiptOut])
async def list_receipts(
    actor: CurrentActor,
    session: OrgSession,
    po_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> list[ReceiptOut]:
    authorize(actor, PURCHASE_ORDER_READ)
    query = select(PayGoodsReceipt)
    if po_id is not None:
        query = query.where(PayGoodsReceipt.po_id == po_id)
    rows = await session.scalars(
        query.order_by(PayGoodsReceipt.created_at.desc(), PayGoodsReceipt.id).limit(limit)
    )
    return [_receipt(r) for r in rows]


@router.post("/goods-receipts", status_code=201, response_model=ReceiptOut)
async def create_receipt(
    request: Request, body: GoodsReceiptIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, PURCHASE_ORDER_WRITE)
    data = ReceiptInput(
        grn_number=body.grn_number,
        po_id=body.po_id,
        received_date=body.received_date,
        received_by=body.received_by,
        lines=tuple(ReceiptLineInput(x.po_line_no, x.quantity) for x in body.lines),
    )
    try:
        receipt = await create_goods_receipt(session, By.of(actor), data)
    except (InvalidPurchaseDataError, PurchaseNumberTakenError) as exc:
        return _problem(request, exc)
    return _receipt(receipt)


@router.post("/goods-receipts/import", response_model=ImportOut)
async def import_receipts(
    request: Request, body: CsvIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, PURCHASE_ORDER_WRITE)
    try:
        result = await import_goods_receipts(session, By.of(actor), body.csv)
    except (InvalidPurchaseDataError, PurchaseNumberTakenError) as exc:
        return _problem(request, exc)
    return ImportOut(created=len(result.created), ids=result.created, numbers=result.numbers)

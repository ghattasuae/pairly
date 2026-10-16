# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Purchase orders and goods receipts (brief 4.5.1), the other side of matching
(``pairly_pay.checks.matching``).

* A **purchase order** names a vendor of the organisation, a currency, an order date,
  whether invoices match it 2-way (against the PO) or 3-way (against the PO and the
  goods received), and its lines: description, quantity, unit price (in the PO's
  currency). Line totals and the subtotal are computed here (``Decimal``, rounded half
  up to the currency's minor unit), never taken from the input.
* A **goods receipt** (GRN) records the quantities received of a PO's lines. A PO line
  can never be received more than it was ordered (counting every earlier receipt), and
  a cancelled or closed PO receives nothing.

A person **closes** a PO (``close_purchase_order``: it takes no more invoices; the
invoices received before keep their match) or **cancels** it (``cancel_purchase_order``:
the order did not happen; refused while an invoice matched with it is approved,
scheduled or paid). Either way a closed or cancelled PO fails matching for later
invoices with a clear reason (``effective_status``), and receives no goods.

Both are created from JSON (``create_purchase_order``, ``create_goods_receipt``) or
imported from CSV (``import_purchase_orders``, ``import_goods_receipts``). Validation
is strict: every value is checked (plain decimals only, no signs, exponents or
thousands separators; ISO dates; known columns only; no cell that a spreadsheet would
run as a formula), every problem is reported with its row, and an import with any
problem creates nothing. Numbers are unique per organisation, compared without case,
spaces or separators (``number_key``), so ``PO-2026-0079`` and ``po 2026 0079`` are one
PO. Every creation is audited; every query is org scoped (Row Level Security).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_pay.checks.settings import to_minor_unit
from pairly_pay.common import By, audit, clean_text
from pairly_pay.models import (
    InvoiceStatus,
    MatchType,
    PayGoodsReceipt,
    PayGoodsReceiptLine,
    PayInvoice,
    PayPurchaseOrder,
    PayPurchaseOrderLine,
    PayVendor,
    PoStatus,
    RecordSource,
    VendorStatus,
)

PO_ENTITY: Final = "pay.purchase_order"
RECEIPT_ENTITY: Final = "pay.goods_receipt"
MAX_LINES: Final = 500
MAX_CSV_BYTES: Final = 1_000_000
MAX_CSV_ROWS: Final = 5000
MAX_ERRORS: Final = 50
MAX_AMOUNT: Final = Decimal("1000000000000")
_DECIMAL: Final = re.compile(r"^\d{1,13}(?:\.\d{1,4})?$")
_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")
_NUMBER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./#_-]{0,63}$")
_KEY_DROP: Final = re.compile(r"[\s./#_-]+")
# A cell a spreadsheet would run as a formula (CSV injection).
_FORMULA: Final = ("=", "+", "@", "\t", "\r")
_USABLE_VENDOR: Final = frozenset({VendorStatus.ACTIVE.value, VendorStatus.PENDING.value})

PO_COLUMNS: Final = (
    "po_number",
    "vendor_reference",
    "currency",
    "order_date",
    "match_type",
    "department",
    "requester",
    "line_no",
    "description",
    "quantity",
    "unit_price",
)
PO_REQUIRED: Final = frozenset(
    {
        "po_number",
        "vendor_reference",
        "currency",
        "order_date",
        "line_no",
        "description",
        "quantity",
        "unit_price",
    }
)
RECEIPT_COLUMNS: Final = (
    "grn_number",
    "po_number",
    "received_date",
    "received_by",
    "po_line_no",
    "quantity",
)
RECEIPT_REQUIRED: Final = frozenset(
    {"grn_number", "po_number", "received_date", "po_line_no", "quantity"}
)


# ------------------------------------------------------------------ errors


@dataclass(frozen=True, slots=True)
class RowError:
    row: int  # 1 is the header; 0 for a problem with the whole input
    field: str | None
    code: str

    def as_json(self) -> dict[str, Any]:
        return {"row": self.row, "field": self.field, "code": self.code}


class PurchaseOrderNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("purchase order not found")


class PurchaseOrderStateError(ConflictError):
    code = "purchase_order_state"
    title = "The purchase order cannot change now"


class InvalidPurchaseDataError(BadRequestError):
    """The input is not valid; ``errors`` lists every problem (row 0 for JSON input)."""

    code = "invalid_purchase_data"
    title = "Invalid purchase order or receipt"

    def __init__(self, errors: Sequence[RowError]) -> None:
        listed = list(errors)[:MAX_ERRORS]
        super().__init__(
            "the input has problems: "
            + ", ".join(f"{e.row}:{e.field or '-'}:{e.code}" for e in listed[:10])
        )
        self.errors = listed


class PurchaseNumberTakenError(ConflictError):
    code = "number_taken"
    title = "Number already on file"

    def __init__(self, numbers: Sequence[str]) -> None:
        super().__init__("already on file: " + ", ".join(list(numbers)[:20]))
        self.numbers = list(numbers)


# ------------------------------------------------------------------ inputs


def number_key(value: str | None) -> str | None:
    """A PO or GRN number without case, spaces or separators (``PO20260079``)."""
    key = _KEY_DROP.sub("", (value or "").upper())[:64]
    return key or None


@dataclass(frozen=True, slots=True)
class PoLineInput:
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class PoInput:
    po_number: str
    vendor_id: uuid.UUID
    currency: str
    order_date: dt.date
    lines: tuple[PoLineInput, ...]
    match_type: MatchType = MatchType.THREE_WAY
    department: str | None = None
    requester: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiptLineInput:
    po_line_no: int
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ReceiptInput:
    grn_number: str
    po_id: uuid.UUID
    received_date: dt.date
    lines: tuple[ReceiptLineInput, ...]
    received_by: str | None = None


def parse_decimal(value: str | None, *, positive: bool) -> Decimal | None:
    """A plain decimal (digits, at most 4 places), or None when it is not one."""
    text = (value or "").strip()
    if not _DECIMAL.fullmatch(text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number > MAX_AMOUNT or (positive and number <= 0):
        return None
    return number


def _date(value: str | None) -> dt.date | None:
    text = (value or "").strip()
    if not _DATE.fullmatch(text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _formula(value: str) -> bool:
    return value.startswith(_FORMULA)


def _text_errors(row: int, name: str, value: str | None, limit: int) -> list[RowError]:
    if value is None:
        return []
    if _formula(value):
        return [RowError(row, name, "formula_not_allowed")]
    if len(value) > limit:
        return [RowError(row, name, "too_long")]
    return []


def check_number(row: int, name: str, value: str | None) -> list[RowError]:
    text = (value or "").strip()
    if not text:
        return [RowError(row, name, "required")]
    if not _NUMBER.fullmatch(text):
        return [RowError(row, name, "invalid_number")]
    return []


def validate_po(data: PoInput, row: int = 0) -> list[RowError]:
    """Every problem of a PO (``row`` 0: JSON input)."""
    errors = check_number(row, "po_number", data.po_number)
    if not _CURRENCY.fullmatch(data.currency or ""):
        errors.append(RowError(row, "currency", "invalid_currency"))
    for name, value, limit in (
        ("department", data.department, 64),
        ("requester", data.requester, 120),
    ):
        errors += _text_errors(row, name, value, limit)
    if not data.lines:
        errors.append(RowError(row, "lines", "required"))
    if len(data.lines) > MAX_LINES:
        errors.append(RowError(row, "lines", "too_many"))
    seen: set[int] = set()
    for line in data.lines:
        if line.line_no <= 0 or line.line_no > 100_000:
            errors.append(RowError(row, "line_no", "invalid_line_no"))
        if line.line_no in seen:
            errors.append(RowError(row, "line_no", "duplicate_line_no"))
        seen.add(line.line_no)
        if not line.description.strip():
            errors.append(RowError(row, "description", "required"))
        errors += _text_errors(row, "description", line.description, 500)
        if not (Decimal(0) < line.quantity <= MAX_AMOUNT) or _places(line.quantity) > 4:
            errors.append(RowError(row, "quantity", "invalid_quantity"))
        if not (Decimal(0) <= line.unit_price <= MAX_AMOUNT) or _places(line.unit_price) > 4:
            errors.append(RowError(row, "unit_price", "invalid_unit_price"))
    return errors


def validate_receipt(data: ReceiptInput, row: int = 0) -> list[RowError]:
    errors = check_number(row, "grn_number", data.grn_number)
    errors += _text_errors(row, "received_by", data.received_by, 120)
    if not data.lines:
        errors.append(RowError(row, "lines", "required"))
    if len(data.lines) > MAX_LINES:
        errors.append(RowError(row, "lines", "too_many"))
    seen: set[int] = set()
    for line in data.lines:
        if line.po_line_no <= 0 or line.po_line_no > 100_000:
            errors.append(RowError(row, "po_line_no", "invalid_line_no"))
        if line.po_line_no in seen:
            errors.append(RowError(row, "po_line_no", "duplicate_line_no"))
        seen.add(line.po_line_no)
        if not (Decimal(0) < line.quantity <= MAX_AMOUNT) or _places(line.quantity) > 4:
            errors.append(RowError(row, "quantity", "invalid_quantity"))
    return errors


def _places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


# ------------------------------------------------------------------ reading


async def get_purchase_order(
    session: AsyncSession, po_id: uuid.UUID, *, lock: bool = False
) -> PayPurchaseOrder:
    query = select(PayPurchaseOrder).where(PayPurchaseOrder.id == po_id)
    if lock:
        query = query.with_for_update()
    po = await session.scalar(query)
    if po is None:
        raise PurchaseOrderNotFoundError()
    return po


async def find_purchase_order(
    session: AsyncSession, number: str | None, *, lock: bool = False
) -> PayPurchaseOrder | None:
    """The PO with this number (compared by ``number_key``), or None."""
    key = number_key(number)
    if key is None:
        return None
    query = select(PayPurchaseOrder).where(PayPurchaseOrder.number_key == key)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def po_lines(session: AsyncSession, po_id: uuid.UUID) -> list[PayPurchaseOrderLine]:
    rows = await session.scalars(
        select(PayPurchaseOrderLine)
        .where(PayPurchaseOrderLine.po_id == po_id)
        .order_by(PayPurchaseOrderLine.line_no)
    )
    return list(rows)


async def receipts_of(session: AsyncSession, po_id: uuid.UUID) -> list[PayGoodsReceipt]:
    rows = await session.scalars(
        select(PayGoodsReceipt)
        .where(PayGoodsReceipt.po_id == po_id)
        .order_by(PayGoodsReceipt.received_date, PayGoodsReceipt.created_at)
    )
    return list(rows)


async def received_quantities(session: AsyncSession, po_id: uuid.UUID) -> dict[int, Decimal]:
    """The quantity received so far per PO line number (every receipt)."""
    rows = await session.execute(
        select(PayGoodsReceiptLine.po_line_no, func.sum(PayGoodsReceiptLine.quantity))
        .where(PayGoodsReceiptLine.po_id == po_id)
        .group_by(PayGoodsReceiptLine.po_line_no)
    )
    return {int(n): Decimal(q) for n, q in rows.all()}


def allocation_of(invoice: PayInvoice) -> dict[int, Decimal]:
    """The quantity per PO line an invoice took (``po_match.lines``)."""
    out: dict[int, Decimal] = {}
    match = invoice.po_match if isinstance(invoice.po_match, Mapping) else {}
    for item in match.get("lines") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            line_no = int(item["po_line_no"])
            quantity = Decimal(str(item["quantity"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if quantity.is_finite() and quantity > 0:
            out[line_no] = out.get(line_no, Decimal(0)) + quantity
    return out


async def invoiced_quantities(
    session: AsyncSession,
    po_id: uuid.UUID,
    *,
    before: PayInvoice | None = None,
) -> dict[int, Decimal]:
    """The quantity per PO line that invoices citing the PO took (rejected ones left
    out). With ``before``: only invoices received before that one (so a later invoice
    never changes an earlier one's result when its checks run again)."""
    query = select(PayInvoice).where(
        PayInvoice.po_id == po_id, PayInvoice.status != InvoiceStatus.REJECTED.value
    )
    if before is not None:
        query = query.where(
            PayInvoice.id != before.id,
            (PayInvoice.created_at < before.created_at)
            | ((PayInvoice.created_at == before.created_at) & (PayInvoice.id < before.id)),
        )
    out: dict[int, Decimal] = {}
    for invoice in await session.scalars(query.limit(1000)):
        for line_no, quantity in allocation_of(invoice).items():
            out[line_no] = out.get(line_no, Decimal(0)) + quantity
    return out


def effective_status(po: PayPurchaseOrder, invoice_created_at: dt.datetime | None) -> str:
    """The PO's status for one invoice: a PO closed after the invoice arrived is still
    open for it (its match stands), so closing never changes an invoice already in
    flight. A cancelled PO is cancelled for every invoice."""
    if (
        po.status == PoStatus.CLOSED.value
        and po.closed_at is not None
        and invoice_created_at is not None
        and invoice_created_at <= po.closed_at
    ):
        return PoStatus.OPEN.value
    return po.status


# ------------------------------------------------------------------ closing

PAID_OR_APPROVED: Final = frozenset(
    {InvoiceStatus.APPROVED.value, InvoiceStatus.SCHEDULED.value, InvoiceStatus.PAID.value}
)


async def _end(
    session: AsyncSession, by: By, po_id: uuid.UUID, status: PoStatus, reason: str
) -> PayPurchaseOrder:
    text_reason = clean_text(reason, 501)
    if not text_reason:
        raise BadRequestError("a reason is required")
    if len(text_reason) > 500:
        raise BadRequestError("the reason is longer than 500 characters")
    po = await get_purchase_order(session, po_id, lock=True)
    if po.status != PoStatus.OPEN.value:
        raise PurchaseOrderStateError(f"the purchase order is {po.status}")
    now = dt.datetime.now(dt.UTC)
    po.status = status.value
    po.closed_at = now
    po.closed_by_type = by.actor_type.value
    po.closed_by_id = by.actor_id
    po.close_reason = text_reason
    po.updated_at = now
    await session.flush()
    await audit(
        session,
        by,
        f"pay.purchase_order.{status.value}",
        entity_type=PO_ENTITY,
        entity_id=po.id,
        payload={"po_number": po.po_number, "reason": text_reason[:200]},
    )
    return po


async def close_purchase_order(
    session: AsyncSession, by: By, po_id: uuid.UUID, reason: str
) -> PayPurchaseOrder:
    """An open PO takes no more invoices (audited). Invoices received before keep their
    match; later ones fail matching (``po_closed``)."""
    return await _end(session, by, po_id, PoStatus.CLOSED, reason)


async def cancel_purchase_order(
    session: AsyncSession, by: By, po_id: uuid.UUID, reason: str
) -> PayPurchaseOrder:
    """The order did not happen (audited). Refused while an invoice matched with it is
    approved, scheduled or paid: that money is committed, close the PO instead. Every
    other invoice citing it fails matching from now on (an invoice awaiting approval
    gets stale checks, so it can no longer be approved)."""
    await get_purchase_order(session, po_id, lock=True)  # before looking at its invoices
    committed = await session.scalar(
        select(PayInvoice.reference)
        .where(PayInvoice.po_id == po_id, PayInvoice.status.in_(sorted(PAID_OR_APPROVED)))
        .limit(1)
    )
    if committed is not None:
        raise PurchaseOrderStateError(
            f"invoice {committed} of this purchase order is approved or paid: close it instead"
        )
    return await _end(session, by, po_id, PoStatus.CANCELLED, reason)


# ------------------------------------------------------------------ creating


async def _vendor(session: AsyncSession, vendor_id: uuid.UUID) -> PayVendor | None:
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == vendor_id))
    return vendor if vendor is not None and vendor.status in _USABLE_VENDOR else None


async def _taken_po_keys(session: AsyncSession, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    rows = await session.scalars(
        select(PayPurchaseOrder.number_key).where(PayPurchaseOrder.number_key.in_(list(keys)))
    )
    return set(rows)


async def _taken_grn_keys(session: AsyncSession, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    rows = await session.scalars(
        select(PayGoodsReceipt.number_key).where(PayGoodsReceipt.number_key.in_(list(keys)))
    )
    return set(rows)


async def _insert_po(
    session: AsyncSession, by: By, data: PoInput, source: RecordSource
) -> PayPurchaseOrder:
    key = number_key(data.po_number)
    if key is None:  # validated before
        raise InvalidPurchaseDataError([RowError(0, "po_number", "required")])
    lines = sorted(data.lines, key=lambda x: x.line_no)
    totals = [to_minor_unit(x.quantity * x.unit_price, data.currency) for x in lines]
    now = dt.datetime.now(dt.UTC)
    po = PayPurchaseOrder(
        id=uuid.uuid4(),
        po_number=data.po_number.strip(),
        number_key=key,
        vendor_id=data.vendor_id,
        status=PoStatus.OPEN.value,
        match_type=data.match_type.value,
        currency=data.currency,
        order_date=data.order_date,
        department=clean_text(data.department, 64) or None,
        requester=clean_text(data.requester, 120) or None,
        subtotal=sum(totals, Decimal(0)),
        line_count=len(lines),
        source=source.value,
        created_by_type=by.actor_type.value,
        created_by_id=by.actor_id,
        created_at=now,
        updated_at=now,
    )
    session.add(po)
    await session.flush()
    for line, total in zip(lines, totals, strict=True):
        session.add(
            PayPurchaseOrderLine(
                po_id=po.id,
                line_no=line.line_no,
                description=clean_text(line.description, 500),
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=total,
            )
        )
    await session.flush()
    await audit(
        session,
        by,
        "pay.purchase_order.created",
        entity_type=PO_ENTITY,
        entity_id=po.id,
        payload={
            "po_number": po.po_number,
            "vendor_id": str(po.vendor_id),
            "currency": po.currency,
            "match_type": po.match_type,
            "lines": po.line_count,
            "subtotal": format(po.subtotal, "f"),
            "source": source.value,
        },
    )
    return po


async def create_purchase_order(
    session: AsyncSession, by: By, data: PoInput, *, source: RecordSource = RecordSource.API
) -> PayPurchaseOrder:
    """Create one PO (validated, audited). Raises with every problem."""
    errors = validate_po(data)
    if not errors and await _vendor(session, data.vendor_id) is None:
        errors.append(RowError(0, "vendor_id", "vendor_not_found"))
    if errors:
        raise InvalidPurchaseDataError(errors)
    key = number_key(data.po_number)
    if key is not None and await _taken_po_keys(session, [key]):
        raise PurchaseNumberTakenError([data.po_number])
    return await _insert_po(session, by, data, source)


async def _check_receipt_lines(
    session: AsyncSession, po: PayPurchaseOrder, data: ReceiptInput, row: int
) -> list[RowError]:
    if po.status != PoStatus.OPEN.value:
        return [RowError(row, "po_number", f"po_{po.status}")]
    lines = {line.line_no: line for line in await po_lines(session, po.id)}
    received = await received_quantities(session, po.id)
    errors: list[RowError] = []
    for line in data.lines:
        ordered = lines.get(line.po_line_no)
        if ordered is None:
            errors.append(RowError(row, "po_line_no", "po_line_not_found"))
            continue
        if received.get(line.po_line_no, Decimal(0)) + line.quantity > ordered.quantity:
            errors.append(RowError(row, "quantity", "more_than_ordered"))
    return errors


async def _insert_receipt(
    session: AsyncSession, by: By, po: PayPurchaseOrder, data: ReceiptInput, source: RecordSource
) -> PayGoodsReceipt:
    key = number_key(data.grn_number)
    if key is None:  # validated before
        raise InvalidPurchaseDataError([RowError(0, "grn_number", "required")])
    receipt = PayGoodsReceipt(
        id=uuid.uuid4(),
        grn_number=data.grn_number.strip(),
        number_key=key,
        po_id=po.id,
        received_date=data.received_date,
        received_by=clean_text(data.received_by, 120) or None,
        line_count=len(data.lines),
        source=source.value,
        created_by_type=by.actor_type.value,
        created_by_id=by.actor_id,
    )
    session.add(receipt)
    await session.flush()
    for line in sorted(data.lines, key=lambda x: x.po_line_no):
        session.add(
            PayGoodsReceiptLine(
                receipt_id=receipt.id,
                po_id=po.id,
                po_line_no=line.po_line_no,
                quantity=line.quantity,
            )
        )
    po.updated_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await audit(
        session,
        by,
        "pay.goods_receipt.created",
        entity_type=RECEIPT_ENTITY,
        entity_id=receipt.id,
        payload={
            "grn_number": receipt.grn_number,
            "po_id": str(po.id),
            "po_number": po.po_number,
            "lines": receipt.line_count,
            "quantities": {str(x.po_line_no): format(x.quantity, "f") for x in data.lines},
            "source": source.value,
        },
    )
    return receipt


async def create_goods_receipt(
    session: AsyncSession, by: By, data: ReceiptInput, *, source: RecordSource = RecordSource.API
) -> PayGoodsReceipt:
    """Record one goods receipt against an open PO (validated, audited). The PO row is
    locked, so two receipts at once can never receive a line more than it was ordered."""
    errors = validate_receipt(data)
    if errors:
        raise InvalidPurchaseDataError(errors)
    po = await get_purchase_order(session, data.po_id, lock=True)
    errors = await _check_receipt_lines(session, po, data, 0)
    if errors:
        raise InvalidPurchaseDataError(errors)
    key = number_key(data.grn_number)
    if key is not None and await _taken_grn_keys(session, [key]):
        raise PurchaseNumberTakenError([data.grn_number])
    return await _insert_receipt(session, by, po, data, source)


# ------------------------------------------------------------------ CSV


@dataclass
class CsvRows:
    rows: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


def read_csv(content: str | bytes, columns: Sequence[str], required: frozenset[str]) -> CsvRows:
    """The rows of a CSV with a header (row 1), as text, with every format problem."""
    out = CsvRows()
    if isinstance(content, bytes):
        if len(content) > MAX_CSV_BYTES:
            out.errors.append(RowError(0, None, "too_large"))
            return out
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            out.errors.append(RowError(0, None, "not_utf8"))
            return out
    if len(content.encode("utf-8")) > MAX_CSV_BYTES:
        out.errors.append(RowError(0, None, "too_large"))
        return out
    content = content.removeprefix("﻿")
    if "\x00" in content:
        out.errors.append(RowError(0, None, "not_text"))
        return out
    try:
        records = list(csv.reader(io.StringIO(content, newline=""), strict=True))
    except csv.Error:
        out.errors.append(RowError(0, None, "malformed_csv"))
        return out
    if not records:
        out.errors.append(RowError(0, None, "empty"))
        return out
    header = [h.strip().lower() for h in records[0]]
    unknown = [h for h in header if h not in columns]
    out.errors += [RowError(1, h or None, "unknown_column") for h in unknown]
    if len(set(header)) != len(header):
        out.errors.append(RowError(1, None, "duplicate_column"))
    out.errors += [RowError(1, c, "missing_column") for c in sorted(required - set(header))]
    body = [r for r in records[1:] if any(cell.strip() for cell in r)]
    if not body:
        out.errors.append(RowError(0, None, "no_rows"))
    if len(body) > MAX_CSV_ROWS:
        out.errors.append(RowError(0, None, "too_many_rows"))
    if out.errors:
        return out
    for number, record in enumerate(records[1:], start=2):
        if not any(cell.strip() for cell in record):
            continue
        if len(record) != len(header):
            out.errors.append(RowError(number, None, "wrong_column_count"))
            continue
        row = {h: cell.strip() for h, cell in zip(header, record, strict=True)}
        for name in sorted(required):
            if not row.get(name):
                out.errors.append(RowError(number, name, "required"))
        for name, value in row.items():
            if value and _formula(value):
                out.errors.append(RowError(number, name, "formula_not_allowed"))
        out.rows.append((number, row))
    return out


def _int(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 6 else None


@dataclass(frozen=True, slots=True)
class ImportResult:
    created: list[uuid.UUID]
    numbers: list[str]


async def import_purchase_orders(
    session: AsyncSession, by: By, content: str | bytes
) -> ImportResult:
    """POs from CSV, one row per line (``PO_COLUMNS``), grouped by ``po_number``; the
    header columns of one PO must agree on every row. All or nothing."""
    parsed = read_csv(content, PO_COLUMNS, PO_REQUIRED)
    errors = list(parsed.errors)
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    numbers: dict[str, str] = {}
    for number, row in parsed.rows:
        key = number_key(row.get("po_number"))
        if key is None:
            continue
        groups.setdefault(key, []).append((number, row))
        numbers.setdefault(key, row["po_number"])
    references = sorted({row.get("vendor_reference", "") for _, row in parsed.rows})
    vendors: dict[str, PayVendor] = {}
    if references:
        rows = await session.scalars(
            select(PayVendor).where(PayVendor.reference.in_(references[:1000]))
        )
        vendors = {v.reference: v for v in rows if v.status in _USABLE_VENDOR}
    inputs: list[PoInput] = []
    header = ("vendor_reference", "currency", "order_date", "match_type", "department",
              "requester")  # fmt: skip
    for rows_of in groups.values():
        first_row, first = rows_of[0]
        for number, row in rows_of[1:]:
            for name in header:
                if row.get(name, "") != first.get(name, ""):
                    errors.append(RowError(number, name, "differs_within_po"))
        vendor = vendors.get(first.get("vendor_reference", ""))
        if vendor is None:
            errors.append(RowError(first_row, "vendor_reference", "vendor_not_found"))
        order_date = _date(first.get("order_date"))
        if order_date is None:
            errors.append(RowError(first_row, "order_date", "invalid_date"))
        match_text = first.get("match_type") or MatchType.THREE_WAY.value
        if match_text not in {m.value for m in MatchType}:
            errors.append(RowError(first_row, "match_type", "invalid_match_type"))
        lines: list[PoLineInput] = []
        for number, row in rows_of:
            line_no = _int(row.get("line_no"))
            quantity = parse_decimal(row.get("quantity"), positive=True)
            price = parse_decimal(row.get("unit_price"), positive=False)
            if line_no is None:
                errors.append(RowError(number, "line_no", "invalid_line_no"))
            if quantity is None:
                errors.append(RowError(number, "quantity", "invalid_quantity"))
            if price is None:
                errors.append(RowError(number, "unit_price", "invalid_unit_price"))
            if line_no is not None and quantity is not None and price is not None:
                lines.append(PoLineInput(line_no, row.get("description", ""), quantity, price))
        if vendor is None or order_date is None or match_text not in {m.value for m in MatchType}:
            continue
        data = PoInput(
            po_number=first["po_number"],
            vendor_id=vendor.id,
            currency=first.get("currency", ""),
            order_date=order_date,
            lines=tuple(lines),
            match_type=MatchType(match_text),
            department=first.get("department") or None,
            requester=first.get("requester") or None,
        )
        errors += validate_po(data, first_row)
        inputs.append(data)
    taken = await _taken_po_keys(session, list(groups))
    errors += [RowError(groups[k][0][0], "po_number", "number_taken") for k in sorted(taken)]
    if errors:
        raise InvalidPurchaseDataError(sorted(errors, key=lambda e: (e.row, e.field or "")))
    created = [await _insert_po(session, by, data, RecordSource.CSV) for data in inputs]
    return ImportResult([po.id for po in created], [po.po_number for po in created])


async def import_goods_receipts(
    session: AsyncSession, by: By, content: str | bytes
) -> ImportResult:
    """Goods receipts from CSV, one row per PO line received (``RECEIPT_COLUMNS``),
    grouped by ``grn_number``. All or nothing; each PO is locked while it is checked."""
    parsed = read_csv(content, RECEIPT_COLUMNS, RECEIPT_REQUIRED)
    errors = list(parsed.errors)
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for number, row in parsed.rows:
        key = number_key(row.get("grn_number"))
        if key is not None:
            groups.setdefault(key, []).append((number, row))
    planned: list[tuple[PayPurchaseOrder, ReceiptInput]] = []
    # Quantities this import adds per (PO, line), on top of earlier receipts.
    adding: dict[tuple[uuid.UUID, int], Decimal] = {}
    for rows_of in groups.values():
        first_row, first = rows_of[0]
        for number, row in rows_of[1:]:
            for name in ("po_number", "received_date", "received_by"):
                if row.get(name, "") != first.get(name, ""):
                    errors.append(RowError(number, name, "differs_within_receipt"))
        po = await find_purchase_order(session, first.get("po_number"), lock=True)
        if po is None:
            errors.append(RowError(first_row, "po_number", "po_not_found"))
        received_date = _date(first.get("received_date"))
        if received_date is None:
            errors.append(RowError(first_row, "received_date", "invalid_date"))
        lines: list[ReceiptLineInput] = []
        for number, row in rows_of:
            line_no = _int(row.get("po_line_no"))
            quantity = parse_decimal(row.get("quantity"), positive=True)
            if line_no is None:
                errors.append(RowError(number, "po_line_no", "invalid_line_no"))
            if quantity is None:
                errors.append(RowError(number, "quantity", "invalid_quantity"))
            if line_no is not None and quantity is not None:
                lines.append(ReceiptLineInput(line_no, quantity))
        if po is None or received_date is None:
            continue
        data = ReceiptInput(
            grn_number=first["grn_number"],
            po_id=po.id,
            received_date=received_date,
            lines=tuple(lines),
            received_by=first.get("received_by") or None,
        )
        found = validate_receipt(data, first_row)
        if not found:
            # Earlier rows of this import count as received for the later ones.
            shifted = ReceiptInput(
                data.grn_number,
                po.id,
                received_date,
                tuple(
                    ReceiptLineInput(
                        x.po_line_no, x.quantity + adding.get((po.id, x.po_line_no), Decimal(0))
                    )
                    for x in data.lines
                ),
            )
            found = await _check_receipt_lines(session, po, shifted, first_row)
            for x in data.lines:
                adding[(po.id, x.po_line_no)] = (
                    adding.get((po.id, x.po_line_no), Decimal(0)) + x.quantity
                )
        errors += found
        planned.append((po, data))
    taken = await _taken_grn_keys(session, list(groups))
    errors += [RowError(groups[k][0][0], "grn_number", "number_taken") for k in sorted(taken)]
    if errors:
        raise InvalidPurchaseDataError(sorted(errors, key=lambda e: (e.row, e.field or "")))
    created = [
        await _insert_receipt(session, by, po, data, RecordSource.CSV) for po, data in planned
    ]
    return ImportResult([r.id for r in created], [r.grn_number for r in created])

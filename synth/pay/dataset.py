# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Build the synthetic Pay dataset: vendors, regular invoices with purchase orders,
goods receipts and matching cases, attack invoices and inbound emails.

``build(count, seed)`` returns a ``Dataset``: every manifest record plus a lazy
``render()`` per document, so the manifests can be rebuilt and checked without
rendering a file (``python -m synth.pay --check``).

Matching tolerances (stated in ``manifests/summary.json``): unit price within 2% of
the PO, quantity within 5% of the received quantity (3-way) or the ordered quantity
(2-way). The tolerance cases sit just inside (1.9%, 4%) and just outside (2.2%, 6%).
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from pairly_core.decisions.bidi import has_rtl
from synth.hire.facts import stable_seed
from synth.pay.catalog import CATEGORY_BY_KEY, DEPARTMENTS, FIRST_NAMES, LAST_NAMES
from synth.pay.invoices import (
    InvoiceDoc,
    LineSpec,
    invoice_number,
    line_truths,
    make_truth,
    money,
    pick_lines,
    q2,
    qty_str,
    vendor_prefix,
)
from synth.pay.models import (
    AttackInfo,
    EmailRecord,
    GoodsReceipt,
    InvoiceRecord,
    MatchCase,
    PoLine,
    PurchaseOrder,
    ReceiptLine,
    Vendor,
)
from synth.pay.render import render
from synth.pay.vendors import make_vendors

DEFAULT_COUNT: Final = 320
DEFAULT_SEED: Final = 2026
START: Final = dt.date(2026, 1, 5)
END: Final = dt.date(2026, 9, 25)
TOLERANCES: Final = {"price_pct": "2.0", "quantity_pct": "5.0"}
GOODS_SCENARIOS: Final = (
    "exact",
    "price_inside",
    "qty_inside",
    "partial_matched",
    "missing_receipt",
    "exact",
    "price_outside",
    "qty_outside",
    "multi_invoice",
    "partial_overbilled",
    "exact",
    "unknown_po",
)
SERVICE_SCENARIOS: Final = ("exact", "price_inside", "exact", "price_outside", "multi_invoice")
EXTENSIONS: Final = {
    "pdf": "pdf",
    "png": "png",
    "jpg": "jpg",
    "scan_pdf": "pdf",
    "docx": "docx",
    "xml": "xml",
    "email_body": "txt",
}


@dataclass
class DocSpec:
    record: InvoiceRecord
    doc: InvoiceDoc

    def render(self) -> bytes:
        return render(self.doc)


@dataclass
class Dataset:
    count: int
    seed: int
    vendors: list[Vendor]
    unknown_vendors: list[Vendor] = field(default_factory=list)
    docs: list[DocSpec] = field(default_factory=list)
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)
    receipts: list[GoodsReceipt] = field(default_factory=list)
    cases: list[MatchCase] = field(default_factory=list)
    emails: list[EmailRecord] = field(default_factory=list)

    def doc(self, doc_id: str) -> DocSpec:
        for spec in self.docs:
            if spec.record.id == doc_id:
                return spec
        raise KeyError(doc_id)

    def vendor(self, vendor_id: str) -> Vendor:
        for v in (*self.vendors, *self.unknown_vendors):
            if v.id == vendor_id:
                return v
        raise KeyError(vendor_id)

    def regular(self) -> Iterator[DocSpec]:
        return (s for s in self.docs if s.record.kind == "invoice")


def weighted(rng: random.Random, weights: dict[str, int]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def text_layer(fmt: str, doc: InvoiceDoc) -> str:
    if fmt in ("png", "jpg", "scan_pdf"):
        return "image"
    if fmt == "xml":
        return "structured"
    if fmt == "pdf" and (doc.language != "en" or has_rtl(doc.truth.vendor_name)):
        return "visual_shaped"
    return "logical"


def make_record(
    doc: InvoiceDoc,
    kind: str,
    *,
    channel: str,
    usual_template: bool = True,
    attack: AttackInfo | None = None,
    demo: str | None = None,
) -> InvoiceRecord:
    folder = "invoices" if kind == "invoice" else "attacks"
    file = doc.filename or f"{doc.id}.{EXTENSIONS[doc.format]}"
    return InvoiceRecord(
        id=doc.id,
        kind=kind,  # type: ignore[arg-type]
        file=f"{folder}/{file}",
        format=doc.format,  # type: ignore[arg-type]
        layout=doc.layout,  # type: ignore[arg-type]
        language=doc.language,  # type: ignore[arg-type]
        text_layer=text_layer(doc.format, doc),  # type: ignore[arg-type]
        channel=channel,  # type: ignore[arg-type]
        email_id=None,
        date_format=doc.date_format,
        usual_template=usual_template,
        demo=demo,
        truth=doc.truth,
        attack=attack,
    )


def new_doc(
    doc_id: str,
    vendor: Vendor,
    fmt: str,
    seed: int,
    *,
    number: str,
    date: dt.date,
    specs: list[LineSpec],
    po_number: str | None,
    language: str | None = None,
    seq: int = 0,
) -> InvoiceDoc:
    t = vendor.template
    language = language or t.language
    bank = vendor.bank if (t.prints_bank or fmt == "xml") else None
    truth = make_truth(
        vendor,
        number=number,
        date=date,
        lines=line_truths(specs, language),
        language=language,
        po_number=po_number,
        bank=bank,
    )
    layout = {"xml": "ubl", "email_body": "plain"}.get(fmt, t.layout)
    return InvoiceDoc(
        id=doc_id,
        vendor=vendor,
        truth=truth,
        format=fmt,
        layout=layout,
        language=language,
        font=t.font,
        accent=t.accent,
        logo=t.logo,
        date_format=t.date_format,
        producer=t.producer,
        seed=seed,
        seq=seq,
    )


def allocate(count: int, vendors: list[Vendor], rng: random.Random) -> list[int]:
    base = min(5, max(1, count // (len(vendors) * 2)))
    alloc = [base] * len(vendors)
    weights = [3 if v.country == "AE" else 1 for v in vendors]
    for k in rng.choices(range(len(vendors)), weights=weights, k=count - base * len(vendors)):
        alloc[k] += 1
    return alloc


def pick_format(rng: random.Random, vendor: Vendor) -> str:
    fmt = weighted(rng, vendor.template.formats)
    if fmt == "scan":
        fmt = weighted(rng, {"png": 45, "jpg": 35, "scan_pdf": 20})
    if fmt in ("pdf", "docx") and vendor.typical_subtotal[0] == "800.00" and rng.random() < 0.15:
        fmt = "email_body"
    return fmt


def pick_channel(rng: random.Random, fmt: str) -> str:
    if fmt == "email_body":
        return "email"
    if fmt == "xml":
        return "api" if rng.random() < 0.7 else "email"
    return "email" if rng.random() < 0.75 else "upload"


def _person(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


class _Counter:
    def __init__(self) -> None:
        self.po = 0
        self.grn = 0
        self.case = 0
        self.goods = 0
        self.services = 0


def _variance_line(specs: list[LineSpec]) -> int:
    """Index of the cheapest countable line (where a quantity variance makes sense)."""
    countable = [i for i, s in enumerate(specs) if s.item.qty in ("int", "bulk")]
    if not countable:
        return 0
    return min(countable, key=lambda i: specs[i].unit_price)


def _po(
    ds: Dataset,
    counter: _Counter,
    vendor: Vendor,
    specs: list[LineSpec],
    date: dt.date,
    rng: random.Random,
    *,
    goods: bool,
) -> PurchaseOrder:
    counter.po += 1
    lines = tuple(
        PoLine(
            line_no=n,
            description=s.item.en,
            quantity=qty_str(s.quantity),
            unit=s.item.unit,
            unit_price=money(s.unit_price),
            line_total=money(s.quantity * s.unit_price),
        )
        for n, s in enumerate(specs, start=1)
    )
    po = PurchaseOrder(
        po_number=f"PO-2026-{counter.po:04d}",
        vendor_id=vendor.id,
        date=(date - dt.timedelta(days=rng.randint(6, 30))).isoformat(),
        currency=vendor.template.currency,
        department=rng.choice(DEPARTMENTS),
        requester=_person(rng),
        kind="goods" if goods else "services",
        match_type="3-way" if goods else "2-way",
        lines=lines,
        subtotal=money(sum((Decimal(x.line_total) for x in lines), Decimal(0))),
    )
    ds.purchase_orders.append(po)
    return po


def _grn(
    ds: Dataset,
    counter: _Counter,
    po: PurchaseOrder,
    quantities: list[Decimal],
    before: dt.date,
    rng: random.Random,
) -> str:
    counter.grn += 1
    po_date = dt.date.fromisoformat(po.date)
    span = max(1, (before - po_date).days - 1)
    grn = GoodsReceipt(
        grn_number=f"GRN-2026-{counter.grn:04d}",
        po_number=po.po_number,
        date=(po_date + dt.timedelta(days=rng.randint(1, span))).isoformat(),
        received_by=_person(rng),
        lines=tuple(
            ReceiptLine(po_line_no=n, quantity_received=qty_str(q))
            for n, q in enumerate(quantities, start=1)
            if q > 0
        ),
    )
    ds.receipts.append(grn)
    return grn.grn_number


def _case(
    ds: Dataset,
    counter: _Counter,
    scenario: str,
    po_number: str | None,
    invoice_ids: list[str],
    grns: list[str],
    exceptions: tuple[str, ...],
    note: str,
) -> None:
    counter.case += 1
    ds.cases.append(
        MatchCase(
            id=f"match-{counter.case:03d}",
            scenario=scenario,
            po_number=po_number,
            invoice_ids=tuple(invoice_ids),
            grn_numbers=tuple(grns),
            expected="exception" if exceptions else "match",
            expected_exceptions=exceptions,
            note=note,
        )
    )


def _scaled(price: Decimal, factor: str, *, above: bool) -> Decimal:
    """A price ``factor`` above the PO price, nudged so the ratio stays on its side of 2%."""
    new = q2(price * Decimal(factor))
    limit = price * Decimal("1.02")
    while above and new <= limit:
        new += Decimal("0.01")
    while not above and new > limit:
        new -= Decimal("0.01")
    return new


def regular_invoices(ds: Dataset) -> None:
    seed, count = ds.seed, ds.count
    rng_all = random.Random(stable_seed(seed, "allocate"))
    alloc = allocate(count, ds.vendors, rng_all)
    counter = _Counter()
    n = 0
    for vendor, k in zip(ds.vendors, alloc, strict=True):
        rng = random.Random(stable_seed(seed, "invoices", vendor.id))
        span = (END - START).days
        dates = sorted(START + dt.timedelta(days=rng.randint(0, span)) for _ in range(k))
        seq = rng.randint(120, 3400)
        low, high = (float(x) for x in vendor.typical_subtotal)
        goods = CATEGORY_BY_KEY[vendor.category].goods
        j = 0
        while j < len(dates):
            date = dates[j]
            seq += rng.randint(1, 4)
            target = Decimal(str(round(rng.uniform(low, high), 2)))
            specs = pick_lines(rng, vendor, target)
            fmt = pick_format(rng, vendor)
            scenario = None
            if vendor.po_based:
                if rng.random() < 0.8:
                    if goods:
                        scenario = GOODS_SCENARIOS[counter.goods % len(GOODS_SCENARIOS)]
                        counter.goods += 1
                    else:
                        scenario = SERVICE_SCENARIOS[counter.services % len(SERVICE_SCENARIOS)]
                        counter.services += 1
                    if scenario == "multi_invoice" and j + 1 >= len(dates):
                        scenario = "exact"
                else:
                    scenario = "no_po"
            if scenario is None or scenario == "no_po":
                n += 1
                doc_id = f"inv-{n:04d}"
                number = invoice_number(vendor.template.numbering, vendor_prefix(vendor), seq, date)
                doc = new_doc(
                    doc_id,
                    vendor,
                    fmt,
                    seed,
                    number=number,
                    date=date,
                    specs=specs,
                    po_number=None,
                    seq=seq,
                )
                ds.docs.append(
                    DocSpec(make_record(doc, "invoice", channel=pick_channel(rng, fmt)), doc)
                )
                if scenario == "no_po":
                    _case(
                        ds,
                        counter,
                        "no_po",
                        None,
                        [doc_id],
                        [],
                        ("no_po",),
                        "Vendor normally invoices against a PO; this invoice cites none.",
                    )
                j += 1
                continue
            n, j, seq = _po_invoices(
                ds, counter, vendor, rng, scenario, specs, dates, j, n, seq, fmt, goods
            )


def _po_invoices(
    ds: Dataset,
    counter: _Counter,
    vendor: Vendor,
    rng: random.Random,
    scenario: str,
    specs: list[LineSpec],
    dates: list[dt.date],
    j: int,
    n: int,
    seq: int,
    fmt: str,
    goods: bool,
) -> tuple[int, int, int]:
    seed = ds.seed
    date = dates[j]
    v = _variance_line(specs)
    if (
        scenario == "multi_invoice"
        and len(specs) < 2
        and (not goods or specs[v].item.qty not in ("int", "bulk"))
    ):
        scenario = "exact"
    if (
        scenario in ("qty_inside", "qty_outside", "partial_matched", "partial_overbilled")
        or (scenario == "multi_invoice" and goods)
    ) and specs[v].item.qty in ("int", "bulk"):
        specs[v] = LineSpec(
            specs[v].item,
            Decimal(100 if specs[v].unit_price < 200 else 50),
            specs[v].unit_price,
            specs[v].vat,
        )
    elif scenario in ("qty_inside", "qty_outside", "partial_matched", "partial_overbilled"):
        scenario = "exact"
    po = _po(ds, counter, vendor, specs, date, rng, goods=goods)
    ordered = [s.quantity for s in specs]
    inv_specs = [LineSpec(s.item, s.quantity, s.unit_price, s.vat) for s in specs]
    received: list[Decimal] | None = list(ordered) if goods else None
    exceptions: tuple[str, ...] = ()
    note = ""
    q = ordered[v]
    if scenario == "exact":
        note = "Invoice equals the PO" + (" and the receipt." if goods else " (2-way, services).")
    elif scenario in ("price_inside", "price_outside"):
        inside = scenario == "price_inside"
        old = specs[v].unit_price
        new = _scaled(old, "1.019" if inside else "1.022", above=not inside)
        inv_specs[v] = LineSpec(specs[v].item, specs[v].quantity, new, specs[v].vat)
        pct = (new - old) / old * 100
        note = f"Line {v + 1} unit price {new} vs PO {old} ({pct:.2f}%), tolerance 2%."
        if not inside:
            exceptions = ("price_variance",)
    elif scenario in ("qty_inside", "qty_outside"):
        extra = Decimal("0.04") if scenario == "qty_inside" else Decimal("0.06")
        billed = q + (q * extra).quantize(Decimal(1))
        inv_specs[v] = LineSpec(specs[v].item, billed, specs[v].unit_price, specs[v].vat)
        note = f"Line {v + 1} invoiced {billed} vs received {q} ({extra * 100:.0f}%), tolerance 5%."
        if scenario == "qty_outside":
            exceptions = ("quantity_variance",)
    elif scenario in ("partial_matched", "partial_overbilled"):
        got = (q * Decimal("0.6")).quantize(Decimal(1))
        if received is None:  # pragma: no cover (partial deliveries are goods only)
            raise RuntimeError("partial delivery without a receipt")
        received[v] = got
        if scenario == "partial_matched":
            inv_specs[v] = LineSpec(specs[v].item, got, specs[v].unit_price, specs[v].vat)
            note = f"Partial delivery: {got} of {q} received and invoiced; PO stays open."
        else:
            note = f"Partial delivery: {got} of {q} received but {q} invoiced."
            exceptions = ("quantity_variance",)
    elif scenario == "missing_receipt":
        received = None
        note = "Goods PO with no goods receipt yet."
        exceptions = ("missing_receipt",)
    elif scenario == "unknown_po":
        ds.purchase_orders.pop()
        counter.po -= 1
        received = None
        note = "Invoice cites a PO number that does not exist."
        exceptions = ("no_po",)
    if scenario == "multi_invoice":
        return _multi(ds, counter, vendor, rng, po, specs, v, dates, j, n, seq, fmt, goods)
    n += 1
    doc_id = f"inv-{n:04d}"
    number = invoice_number(vendor.template.numbering, vendor_prefix(vendor), seq, date)
    po_number = po.po_number if scenario != "unknown_po" else f"PO-2026-9{rng.randint(100, 999)}"
    doc = new_doc(
        doc_id,
        vendor,
        fmt,
        seed,
        number=number,
        date=date,
        specs=inv_specs,
        po_number=po_number,
        seq=seq,
    )
    ds.docs.append(DocSpec(make_record(doc, "invoice", channel=pick_channel(rng, fmt)), doc))
    grns = [_grn(ds, counter, po, received, date, rng)] if received is not None else []
    _case(ds, counter, scenario, po_number, [doc_id], grns, exceptions, note)
    return n, j + 1, seq


def _multi(
    ds: Dataset,
    counter: _Counter,
    vendor: Vendor,
    rng: random.Random,
    po: PurchaseOrder,
    specs: list[LineSpec],
    v: int,
    dates: list[dt.date],
    j: int,
    n: int,
    seq: int,
    fmt: str,
    goods: bool,
) -> tuple[int, int, int]:
    """Two invoices against one PO: a quantity split of one line (goods) or the PO's
    lines split between the two invoices."""
    q = specs[v].quantity
    if goods and specs[v].item.qty in ("int", "bulk") and q >= 2:
        first = (q * Decimal("0.4")).quantize(Decimal(1))
        part_a = list(specs)
        part_a[v] = LineSpec(specs[v].item, first, specs[v].unit_price, specs[v].vat)
        part_b = [LineSpec(specs[v].item, q - first, specs[v].unit_price, specs[v].vat)]
        qty_a = [s.quantity for s in part_a]
        qty_b = [(q - first) if i == v else Decimal(0) for i in range(len(specs))]
        note = f"Two invoices against one PO: {first} then {q - first} of line {v + 1}."
    else:
        part_a, part_b = list(specs[:-1]), [specs[-1]]
        qty_a = [s.quantity for s in specs[:-1]] + [Decimal(0)]
        qty_b = [Decimal(0)] * (len(specs) - 1) + [specs[-1].quantity]
        note = f"Two invoices against one PO: lines 1 to {len(specs) - 1}, then line {len(specs)}."
    ids, grns = [], []
    for k, (part, qty) in enumerate(((part_a, qty_a), (part_b, qty_b))):
        n += 1
        seq += rng.randint(1, 3) if k else 0
        when = dates[j + k]
        doc_id = f"inv-{n:04d}"
        number = invoice_number(vendor.template.numbering, vendor_prefix(vendor), seq, when)
        f = fmt if k == 0 else pick_format(rng, vendor)
        doc = new_doc(
            doc_id,
            vendor,
            f,
            seed=ds.seed,
            number=number,
            date=when,
            specs=part,
            po_number=po.po_number,
            seq=seq,
        )
        ds.docs.append(DocSpec(make_record(doc, "invoice", channel=pick_channel(rng, f)), doc))
        ids.append(doc_id)
        if goods:
            grns.append(_grn(ds, counter, po, qty, when, rng))
    _case(ds, counter, "multi_invoice", po.po_number, ids, grns, (), note)
    return n, j + 2, seq


def build(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> Dataset:
    from synth.pay.attacks import attack_invoices
    from synth.pay.emails import build_emails

    ds = Dataset(count=count, seed=seed, vendors=make_vendors(seed))
    regular_invoices(ds)
    attack_invoices(ds)
    build_emails(ds)
    return ds

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Lay an invoice out as positioned items on one A4 page (points, origin top left).

The same items feed the PDF renderer (reportlab) and the scan renderer (Pillow), so a
scan shows exactly what the PDF would. Layouts:

* ``classic``    logo and vendor top left, title and invoice details top right.
* ``band``       a coloured band across the top with the vendor name in white.
* ``grid``       a boxed line table with a shaded header row and cell borders.
* ``compact``    smaller type, details in one row under the title.
* ``rtl``        Arabic, mirrored right to left (labels, table and blocks).
* ``bilingual``  English layout with Arabic labels beside the English ones.

Hidden attack text is tagged ``hidden`` so the scan renderer can leave it out (a
scan cannot carry invisible text).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Final

from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

from pairly_core.decisions.bidi import has_rtl, shape
from pairly_core.decisions.receipts import FONT_PATH
from synth.pay.banking import format_iban
from synth.pay.catalog import BUYER_ADDRESS
from synth.pay.invoices import InvoiceDoc, print_date, printed_money

W: Final = 595.28
H: Final = 841.89
LEFT: Final = 40.0
RIGHT: Final = W - 40.0
ARABIC_FONT: Final = "PairlyPayDejaVu"
RGB = tuple[float, float, float]

LABELS: Final[dict[str, tuple[str, str]]] = {
    "tax_invoice": ("TAX INVOICE", "فاتورة ضريبية"),
    "invoice": ("INVOICE", "فاتورة"),
    "number": ("Invoice No.", "رقم الفاتورة"),
    "date": ("Invoice date", "تاريخ الفاتورة"),
    "due": ("Due date", "تاريخ الاستحقاق"),
    "terms": ("Payment terms", "شروط الدفع"),
    "po": ("PO number", "رقم أمر الشراء"),
    "currency": ("Currency", "العملة"),
    "bill_to": ("Bill to", "فاتورة إلى"),
    "trn": ("TRN", "رقم التسجيل الضريبي"),
    "no": ("#", "#"),
    "description": ("Description", "الوصف"),
    "qty": ("Qty", "الكمية"),
    "unit": ("Unit", "الوحدة"),
    "unit_price": ("Unit price", "سعر الوحدة"),
    "vat_pct": ("VAT %", "الضريبة %"),
    "vat": ("VAT", "الضريبة"),
    "amount": ("Amount", "المبلغ"),
    "subtotal": ("Subtotal", "المجموع الفرعي"),
    "vat_total": ("VAT", "ضريبة القيمة المضافة"),
    "total": ("Total", "الإجمالي"),
    "bank": ("Bank details", "البيانات المصرفية"),
    "account_name": ("Account name", "اسم الحساب"),
    "bank_name": ("Bank", "البنك"),
    "iban": ("IBAN", "رقم الآيبان"),
    "swift": ("SWIFT", "رمز السويفت"),
    "tel": ("Tel", "هاتف"),
    "reverse": (
        "Reverse charge: VAT to be accounted for by the recipient.",
        "احتساب عكسي: يحتسب المستلم ضريبة القيمة المضافة.",
    ),
    "thanks": ("Thank you for your business.", "شكراً لتعاملكم معنا."),
}


@dataclass(frozen=True)
class Text:
    x: float
    y: float
    text: str
    size: float = 9.0
    bold: bool = False
    align: str = "l"  # l | r | c
    color: RGB = (0.0, 0.0, 0.0)
    tag: str = ""  # "" | hidden | edit_old | edit_new


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    fill: RGB | None = None
    stroke: RGB | None = None
    tag: str = ""


@dataclass(frozen=True)
class Rule:
    x0: float
    y0: float
    x1: float
    y1: float
    color: RGB = (0.6, 0.6, 0.6)
    width: float = 0.6


Item = Text | Box | Rule


def arabic_font() -> str:
    if ARABIC_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(ARABIC_FONT, str(FONT_PATH)))
    return ARABIC_FONT


@cache
def width(text: str, size: float) -> float:
    """Width in the widest font used (DejaVu), so wrapping is safe for every renderer."""
    return float(pdfmetrics.stringWidth(text, arabic_font(), size))


def wrap(text: str, size: float, limit: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and width(candidate, size) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    return [*lines, current] if current else lines or [""]


def visual(text: str) -> str:
    """Shaped, visual-order text; the base direction follows the first strong letter."""
    if not has_rtl(text):
        return text
    for ch in text:
        if ch.isalpha():
            return shape(text, rtl=has_rtl(ch))
    return shape(text)


def hex_rgb(value: str) -> RGB:
    return (int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255)


class _Page:
    def __init__(self, doc: InvoiceDoc) -> None:
        self.doc = doc
        self.rtl = doc.layout == "rtl"
        self.bilingual = doc.layout == "bilingual"
        self.items: list[Item] = []

    def mx(self, x: float) -> float:
        return W - x if self.rtl else x

    def ma(self, align: str) -> str:
        if not self.rtl:
            return align
        return {"l": "r", "r": "l"}.get(align, align)

    def text(self, x: float, y: float, s: str, size: float = 9.0, **kw: object) -> None:
        align = str(kw.pop("align", "l"))
        self.items.append(Text(self.mx(x), y, s, size, align=self.ma(align), **kw))  # type: ignore[arg-type]

    def box(self, x: float, y: float, w: float, h: float, **kw: object) -> None:
        x0 = W - x - w if self.rtl else x
        self.items.append(Box(x0, y, w, h, **kw))  # type: ignore[arg-type]

    def rule(self, x0: float, y: float, x1: float, **kw: object) -> None:
        self.items.append(Rule(self.mx(x0), y, self.mx(x1), y, **kw))  # type: ignore[arg-type]

    def pair(self, x: float, y: float, key: str, value: str, size: float, **kw: object) -> None:
        """``Label: value``; right to left layouts draw the two apart so a Latin value
        keeps its own order."""
        if not self.rtl:
            self.text(x, y, f"{self.label(key)}: {value}", size, **kw)
            return
        label = f"{self.label(key)}:"
        self.text(x, y, label, size, **kw)
        self.text(x + width(label, size) + 4, y, value, size, **kw)

    def label(self, key: str) -> str:
        en, ar = LABELS[key]
        return ar if self.rtl else en

    def labelled(self, x: float, y: float, key: str, size: float, span: float) -> None:
        """A label; bilingual layouts add the Arabic label right-aligned at ``x + span``."""
        self.text(x, y, self.label(key), size, color=(0.35, 0.35, 0.35))
        if self.bilingual:
            self.text(x + span, y, LABELS[key][1], size - 1, align="r", color=(0.35, 0.35, 0.35))


def compose(doc: InvoiceDoc) -> list[Item]:
    page = _Page(doc)
    truth = doc.truth
    vendor = doc.vendor
    accent = hex_rgb(doc.accent)
    compact = doc.layout == "compact"
    base = 8.0 if compact else 9.0
    band = doc.layout == "band"
    grid = doc.layout == "grid"

    # ---------------------------------------------------------------- header
    y = 52.0
    name_x = LEFT
    if band:
        pass
    elif doc.logo != "none":
        if doc.logo == "circle":
            page.box(LEFT, 30, 34, 34, fill=accent, tag="logo_circle")
        elif doc.logo == "square":
            page.box(LEFT, 30, 34, 34, fill=accent)
        else:
            page.box(LEFT, 30, 8, 40, fill=accent)
        name_x = LEFT + (46 if doc.logo != "bar" else 16)
    name_color = (1.0, 1.0, 1.0) if band else accent
    page.text(name_x, y, truth.vendor_name, 15 if not compact else 13, bold=True, color=name_color)
    y += 16
    if page.bilingual and truth.vendor_name_ar:
        page.text(name_x, y, truth.vendor_name_ar, 11, color=name_color)
        y += 14
    sub_color = (0.92, 0.92, 0.92) if band else (0.3, 0.3, 0.3)
    address = vendor.address
    for row in (
        address.line1,
        f"{address.line2}, {address.city}"
        + (f", P.O. Box {address.po_box}" if address.po_box else ""),
    ):
        page.text(name_x, y, row, base - 0.5, color=sub_color)
        y += base + 2
    if truth.vendor_trn:
        page.pair(name_x, y, "trn", truth.vendor_trn, base - 0.5, color=sub_color)
        y += base + 2
    elif vendor.tax_id:
        page.text(name_x, y, f"VAT ID: {vendor.tax_id}", base - 0.5, color=sub_color)
        y += base + 2
    page.pair(
        name_x,
        y,
        "tel",
        f"{vendor.phone_on_file}  {vendor.sender_addresses[0]}",
        base - 0.5,
        color=sub_color,
    )
    y += base + 2
    if band:
        page.items.insert(0, Box(0, 0, W, y + 4, fill=accent))
    title_key = "tax_invoice" if truth.vendor_trn else "invoice"
    title_y = 50.0
    title_color = (1.0, 1.0, 1.0) if band else (0.15, 0.15, 0.15)
    page.text(
        RIGHT,
        title_y,
        page.label(title_key),
        18 if not compact else 15,
        bold=True,
        align="r",
        color=title_color,
    )
    if page.bilingual:
        page.text(RIGHT, title_y + 18, LABELS[title_key][1], 12, align="r", color=title_color)

    # ---------------------------------------------------------------- details
    fmt = doc.date_format
    details: list[tuple[str, str, str]] = [
        ("number", truth.invoice_number, "invoice_number"),
        ("date", print_date(truth.invoice_date, fmt), "invoice_date"),
        ("due", print_date(truth.due_date, fmt), "due_date"),
        ("terms", truth.payment_terms, "payment_terms"),
    ]
    if truth.po_number:
        details.append(("po", truth.po_number, "po_number"))
    details.append(("currency", truth.currency, "currency"))
    top = max(y + 8, 100.0) + 10
    if compact:
        x = LEFT
        for key, value, _field in details:
            page.labelled(x, top, key, base - 1, 80)
            page.text(x, top + base + 2, value, base, bold=True)
            x += 88
        y = top + 34
        bill_y = y
    else:
        span = 118.0 if page.bilingual else 100.0
        dx = 312.0 if page.bilingual else 330.0
        dy = top
        for key, value, _field in details:
            page.labelled(dx, dy, key, base, span)
            page.text(dx + span + 6, dy, value, base, bold=True)
            dy += base + 5
        bill_y = top
        y = dy
    # ---------------------------------------------------------------- bill to
    page.labelled(LEFT, bill_y, "bill_to", base, 120)
    by = bill_y + base + 4
    for row in (truth.buyer_name, *BUYER_ADDRESS):
        page.text(LEFT, by, row, base)
        by += base + 3
    page.pair(LEFT, by, "trn", truth.buyer_trn, base)
    by += base + 3
    y = max(y, by) + 16

    # ---------------------------------------------------------------- lines
    cols = {
        "no": (LEFT, "l"),
        "description": (58.0, "l"),
        "qty": (300.0, "r"),
        "unit": (306.0, "l"),
        "unit_price": (396.0, "r"),
        "vat_pct": (436.0, "r"),
        "vat": (490.0, "r"),
        "amount": (RIGHT, "r"),
    }
    head_h = 16.0 if not page.bilingual else 26.0
    if grid or band:
        page.box(
            LEFT - 4, y - 11, RIGHT - LEFT + 8, head_h, fill=(0.9, 0.92, 0.95) if grid else accent
        )
    head_color = (1.0, 1.0, 1.0) if band else (0.1, 0.1, 0.1)
    for key, (x, align) in cols.items():
        page.text(x, y, page.label(key), base - 0.5, bold=True, align=align, color=head_color)
        if page.bilingual and key != "no":
            page.text(x, y + 10, LABELS[key][1], base - 1.5, align=align, color=head_color)
    y += head_h + 2
    if not grid:
        page.rule(LEFT, y - 10, RIGHT, color=(0.4, 0.4, 0.4))
    table_top = y - 11
    for line in truth.lines:
        desc = wrap(line.description, base, 228 if page.rtl else 225)
        page.text(cols["no"][0], y, str(line.line_no), base)
        for k, part in enumerate(desc):
            page.text(cols["description"][0], y + k * (base + 2), part, base)
        page.text(cols["qty"][0], y, line.quantity, base, align="r")
        page.text(cols["unit"][0], y, line.unit, base - 1)
        page.text(cols["unit_price"][0], y, printed_money(line.unit_price), base, align="r")
        page.text(
            cols["vat_pct"][0], y, f"{line.vat_rate.rstrip('0').rstrip('.')}%", base, align="r"
        )
        page.text(cols["vat"][0], y, printed_money(line.vat_amount), base, align="r")
        page.text(cols["amount"][0], y, printed_money(line.line_total), base, align="r")
        y += max(1, len(desc)) * (base + 2) + 6
        if grid:
            page.rule(LEFT - 4, y - 10, RIGHT + 4, color=(0.7, 0.7, 0.7))
    if grid:
        page.box(LEFT - 4, table_top, RIGHT - LEFT + 8, y - 10 - table_top, stroke=(0.5, 0.5, 0.5))
    else:
        page.rule(LEFT, y - 8, RIGHT, color=(0.4, 0.4, 0.4))
    y += 8

    # ---------------------------------------------------------------- totals
    rate = truth.vat_rate.rstrip("0").rstrip(".")
    rows = [
        ("subtotal", printed_money(truth.subtotal), "subtotal"),
        ("vat_total", printed_money(truth.vat_amount), "vat_amount"),
        ("total", f"{truth.currency} {printed_money(truth.total)}", "total"),
    ]
    ty = y
    for key, value, field_name in rows:
        label = page.label(key)
        if key == "vat_total":
            label = f"{label} {rate}%"
        bold = key == "total"
        page.text(370, ty, label, base + (1 if bold else 0), bold=bold)
        if page.bilingual:
            page.text(370, ty + base + 1, LABELS[key][1], base - 1.5, color=(0.35, 0.35, 0.35))
        tag = ""
        if doc.edit is not None and doc.edit.field == field_name:
            tag = "edit"
        page.text(RIGHT, ty, value, base + (1 if bold else 0), bold=bold, align="r", tag=tag)
        ty += base + (12 if page.bilingual else 6)
    if doc.edit is not None:
        _edit_trace(page, doc, ty)

    # ---------------------------------------------------------------- bank, notes
    ny = y
    if truth.bank is not None:
        page.labelled(LEFT, ny, "bank", base, 150)
        ny += base + 4
        bank = truth.bank
        for key, value in (
            ("account_name", bank.account_name),
            ("bank_name", bank.bank_name),
            ("iban", format_iban(bank.iban)),
            ("swift", bank.bic),
        ):
            page.pair(LEFT, ny, key, value, base - 0.5)
            ny += base + 3
    ny = max(ny, ty) + 14
    notes = list(doc.notes)
    if truth.lines and all(x.vat_category == "O" for x in truth.lines):
        notes.insert(0, page.label("reverse"))
    for note in notes:
        for part in wrap(note, base, RIGHT - LEFT):
            page.text(LEFT, ny, part, base, bold=note in doc.notes)
            ny += base + 3
    page.text(W / 2, H - 40, page.label("thanks"), base - 1, align="c", color=(0.45, 0.45, 0.45))

    for hidden in doc.hidden:
        _hidden(page, hidden.mode, hidden.text, ny)
    return page.items


def _edit_trace(page: _Page, doc: InvoiceDoc, y: float) -> None:
    """The original value sat under the new one: cover it with white, write the new."""
    del y
    edit = doc.edit
    if edit is None:
        return
    for i, item in enumerate(page.items):
        if isinstance(item, Text) and item.tag == "edit":
            old = Text(
                item.x,
                item.y,
                item.text.replace(edit.new, edit.old),
                item.size,
                item.bold,
                item.align,
                item.color,
                "edit_old",
            )
            cover_w = width(old.text, item.size) + 6
            x0 = item.x - cover_w + 3 if item.align == "r" else item.x - 3
            cover = Box(
                x0,
                item.y - item.size,
                cover_w,
                item.size + 4,
                fill=(1.0, 1.0, 1.0),
                tag="edit_cover",
            )
            new = Text(
                item.x, item.y, item.text, item.size, item.bold, item.align, item.color, "edit_new"
            )
            page.items[i : i + 1] = [old, cover, new]
            return


def _hidden(page: _Page, mode: str, text: str, y: float) -> None:
    if mode == "white":
        page.items.append(
            Text(LEFT, min(y + 20, H - 70), text, 8, color=(1.0, 1.0, 1.0), tag="hidden")
        )
    elif mode == "tiny":
        page.items.append(Text(LEFT, H - 55, text, 1, color=(0.7, 0.7, 0.7), tag="hidden"))
    elif mode == "offpage":
        page.items.append(Text(W + 40, 300, text, 8, tag="hidden"))
    elif mode == "behind":
        page.items.append(Text(LEFT, H - 90, text, 8, tag="hidden"))
        page.items.append(
            Box(LEFT - 2, H - 100, width(text, 8) + 6, 14, fill=(1.0, 1.0, 1.0), tag="hidden")
        )


def text_has_rtl(items: list[Item]) -> bool:
    return any(isinstance(i, Text) and has_rtl(i.text) for i in items)

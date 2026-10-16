# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""The offer letter as a PDF (brief 3.8.3), English or Arabic.

ReportLab with the bundled DejaVu Sans font and the engine's Arabic helper
(``pairly_core.decisions.bidi``: contextual shaping and right-to-left ordering), like
the decision receipt. A plain A4 letter: the company, the reference, the opening, the
role and start date, the compensation, the conditions and how to answer (in the
candidate portal: there is no e-signature). Generated on request from the stored offer,
never kept.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Final

from pairly_core.decisions.bidi import has_rtl, shape
from pairly_core.decisions.receipts import FONT_NAME, FONT_PATH
from pairly_hire.offers.service import LetterData

LABELS: Final[dict[str, tuple[str, str]]] = {
    "title": ("Offer of employment", "عرض عمل"),
    "reference": ("Reference", "المرجع"),
    "candidate": ("To", "إلى"),
    "role": ("Role", "الوظيفة"),
    "start": ("Start date", "تاريخ البدء"),
    "type": ("Contract", "العقد"),
    "compensation": ("Compensation (monthly)", "الراتب والبدلات (شهريا)"),
    "base_monthly": ("Basic salary", "الراتب الأساسي"),
    "housing_monthly": ("Housing allowance", "بدل السكن"),
    "transport_monthly": ("Transport allowance", "بدل المواصلات"),
    "other_monthly": ("Other allowances", "بدلات أخرى"),
    "bonus_target_pct": ("Bonus target", "المكافأة المستهدفة"),
    "annual_total": ("Annual total", "الإجمالي السنوي"),
    "conditions": ("Conditions", "الشروط"),
}


def label(key: str, locale: str) -> str:
    en, ar = LABELS[key]
    return ar if locale == "ar" else en


def _font() -> str:
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
    return FONT_NAME


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if stringWidth(candidate, font, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _amount(value: str, currency: str) -> str:
    try:
        number = Decimal(value)
    except ArithmeticError:
        return value
    return f"{currency} {number:,.2f}"


def letter_lines(data: LetterData) -> list[tuple[str, float]]:
    """The letter as (text, font size) lines, in reading order."""
    ar = data.locale == "ar"
    t = data.template
    comp = data.compensation
    currency = comp.get("currency", "")
    opening = (t.opening_ar if ar else t.opening).format(
        job_title=data.job_title, company=data.company
    )
    closing = t.closing_ar if ar else t.closing
    lines: list[tuple[str, float]] = [
        (data.company, 12),
        (label("title", data.locale), 18),
        (f"{label('reference', data.locale)}: {data.reference}", 9),
        (f"{label('candidate', data.locale)}: {data.candidate_name}", 11),
        ("", 6),
        (opening, 11),
        ("", 6),
        (f"{label('role', data.locale)}: {data.job_title}", 11),
        (f"{label('start', data.locale)}: {data.start_date.isoformat()}", 11),
        (f"{label('type', data.locale)}: {t.name_ar if ar else t.name}", 11),
        ("", 6),
        (label("compensation", data.locale), 13),
    ]
    for key in ("base_monthly", "housing_monthly", "transport_monthly", "other_monthly"):
        value = comp.get(key, "0.00")
        if key != "base_monthly" and Decimal(value or "0") == 0:
            continue
        lines.append((f"{label(key, data.locale)}: {_amount(value, currency)}", 11))
    bonus = comp.get("bonus_target_pct", "0.00")
    if Decimal(bonus or "0") > 0:
        lines.append((f"{label('bonus_target_pct', data.locale)}: {Decimal(bonus):.2f}%", 11))
    total = _amount(comp.get("annual_total", "0"), currency)
    lines.append((f"{label('annual_total', data.locale)}: {total}", 11))
    if data.conditions:
        lines.append(("", 6))
        lines.append((label("conditions", data.locale), 13))
        lines += [(f"{n}. {c}", 10) for n, c in enumerate(data.conditions, start=1)]
    lines += [("", 6), (closing, 11)]
    return lines


def render_offer_pdf(data: LetterData) -> bytes:
    """A plain A4 PDF of the offer letter in the offer's language."""
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

    rtl = data.locale == "ar"
    font = _font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=True, pageCompression=1)
    pdf.setTitle(f"{LABELS['title'][0]} {data.reference}")
    pdf.setAuthor(data.company or "Pairly")
    width, height = A4
    margin = 56.0
    usable = width - 2 * margin
    y = height - margin
    for text, size in letter_lines(data):
        if not text:
            y -= size
            continue
        for part in _wrap(text, font, size, usable):
            if y < margin:
                pdf.showPage()
                y = height - margin
            pdf.setFont(font, size)
            if rtl:
                pdf.drawRightString(width - margin, y, shape(part, rtl=True))
            else:
                pdf.drawString(margin, y, shape(part, rtl=False) if has_rtl(part) else part)
            y -= size * 1.5
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()

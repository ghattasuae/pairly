# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A label and its value quoted together from a table read column by column.

The page text holds "Invoice No." and its value on different, distant lines, and the
model quotes them as one passage. The joined quote is no evidence; the value's own
line is, when it is unique in the text and the page layout puts it under or beside
the label. Boxes come from each character's offset in the page text, whatever the
reading order."""

from __future__ import annotations

from pairly_core.documents.model import BBox, Char, NormalizedDocument, Page, ParserInfo, Style
from pairly_core.extraction.engine import FieldAnswer, extract_from_answers
from pairly_core.extraction.evidence import EvidenceLocator

from .conftest import Invoice, answer, guarded

# (text, x0, top): read column by column, as the compact invoice layout is.
CELLS = [
    ("Invoice No.", 40.0, 100.0),
    ("Currency", 200.0, 100.0),
    ("INV-7", 40.0, 110.0),
    ("AED", 200.0, 110.0),
    ("Total", 300.0, 200.0),
    ("Bank details", 40.0, 200.0),
    ("AED 99.00", 380.0, 200.0),
    ("5%", 500.0, 400.0),
]


def _doc(with_boxes: bool = True) -> NormalizedDocument:
    text = "\n".join(c[0] for c in CELLS)
    chars: list[Char] = []
    offset = 0
    for cell, x0, top in CELLS:
        for i, ch in enumerate(cell):
            box = BBox(x0=x0 + 5 * i, top=top, x1=x0 + 5 * (i + 1), bottom=top + 8)
            chars.append(Char(text=ch, bbox=box, style=Style(), offset=offset + i))
        offset += len(cell) + 1
    page = Page(
        number=1,
        width=612.0,
        height=792.0,
        unit="pt",
        text=text,
        chars=chars if with_boxes else [],
    )
    return NormalizedDocument(
        media_type="application/pdf" if with_boxes else "text/plain",
        parser=ParserInfo(name="synthetic", version="1"),
        pages=[page],
    )


def _fields(doc: NormalizedDocument, answers: list[dict[str, object]]) -> dict[str, object]:
    result = extract_from_answers(
        doc,
        guarded(doc),
        Invoice,
        [FieldAnswer.model_validate(a) for a in answers],
        source="test",
        source_version="1",
    )
    return {
        f.path: (f.value, [s.quote for s in f.spans], [x.value for x in f.flags])
        for f in result.fields
        if f.path in {"invoice_number", "currency", "total", "vat_rate"}
    }


ANSWERS = [
    answer("invoice_number", "INV-7", "Invoice No.\nINV-7"),
    answer("currency", "AED", "Currency\nAED"),
    answer("total", "AED 99.00", "Total\nAED 99.00"),
    # "5%" is unique but nowhere near the label: no association, no evidence.
    answer("vat_rate", "5", "Currency\n5%"),
]


def test_value_under_or_beside_its_label_is_evidence() -> None:
    got = _fields(_doc(), ANSWERS)
    assert got["invoice_number"] == ("INV-7", ["INV-7"], [])
    assert got["currency"] == ("AED", ["AED"], [])  # the whole line, not "AED 99.00"
    assert got["total"][1:] == (["AED 99.00"], [])
    assert got["vat_rate"][2] == ["no_evidence"]


def test_a_label_alone_or_a_layout_without_boxes_is_no_evidence() -> None:
    label_only = [answer("currency", "AED", "Currency\nAED 99.00 paid")]
    assert _fields(_doc(), label_only)["currency"][2] == ["no_evidence"]
    got = _fields(_doc(with_boxes=False), ANSWERS)
    assert all(flags == ["no_evidence"] for _, _, flags in got.values())


def test_a_value_line_that_is_not_unique_is_no_evidence() -> None:
    doc = _doc()
    locator = EvidenceLocator(doc, guarded(doc))
    # "AED" is a whole line once (kept); "AE" is in two lines and never a whole one.
    lines = locator.locate_lines("Currency\nAED")
    assert [p.spans[0].quote for p in lines] == ["Currency", "AED"]
    assert [p.spans[0].quote for p in locator.locate_lines("Currency\nAE")] == ["Currency"]


def test_boxes_follow_character_offsets_not_word_order() -> None:
    doc = _doc()
    found = EvidenceLocator(doc, guarded(doc)).locate("AED 99.00")
    assert found is not None
    (box,) = found.spans[0].bboxes
    assert (box.x0, box.top) == (380.0, 200.0)

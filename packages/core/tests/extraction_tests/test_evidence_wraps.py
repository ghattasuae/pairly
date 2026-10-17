# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Evidence quotes over PDF line wraps: whitespace runs, line breaks, soft hyphens and
hyphenated words are tolerated; every other character must still match in order, and
the span returned is the exact original text."""

from __future__ import annotations

from pairly_core.extraction.evidence import EvidenceLocator, normalize

from .conftest import PassThroughInput, make_doc

LINES = [
    "Desert Falcon Trading LLC",
    "Ran month-end close and vendor recon-",
    "ciliations for 40 suppliers with full-",
    "cycle payment runs every week in the",
    "ERP, with three-way match\u00ad",
    "ing on every invoice.",
]


def _locate(quote: str) -> str | None:
    doc = make_doc(LINES)
    found = EvidenceLocator(doc, PassThroughInput.of(doc)).locate(quote)
    if found is None:
        return None
    assert found.spans[0].quote == doc.text[found.start : found.end]
    return doc.text[found.start : found.end]


def test_a_space_in_the_quote_matches_a_line_break() -> None:
    assert _locate("every week in the ERP") == "every week in the\nERP"


def test_a_hyphenated_wrap_matches_the_whole_word() -> None:
    assert _locate("vendor reconciliations for 40") == "vendor recon-\nciliations for 40"
    assert _locate("full-cycle payment runs") == "full-\ncycle payment runs"
    # Copied with the wrap kept as a space still matches.
    assert _locate("vendor recon- ciliations") == "vendor recon-\nciliations"


def test_a_soft_hyphen_at_the_wrap_is_ignored() -> None:
    assert _locate("three-way matching on every invoice") == (
        "three-way match\u00ad\ning on every invoice"
    )


def test_other_characters_must_still_match_in_order() -> None:
    assert _locate("vendor reconcilations for 40") is None  # a letter missing
    assert _locate("fullcycle payment runs every month") is None
    assert _locate("for 40 suppliers with full cycle") is None  # no space in the word
    # A dash between words is not a wrapped word: dropping it is no match.
    doc = make_doc(["Period 2017 -", "2021 at Falcon"])
    locator = EvidenceLocator(doc, PassThroughInput.of(doc))
    assert locator.locate("Period 2017 2021") is None
    assert locator.locate("Period 2017 - 2021") is not None


def test_default_normalisation_is_unchanged() -> None:
    assert normalize("recon-\nciliations").text == "recon- ciliations"
    assert normalize("recon-\nciliations", "keep").text == "recon-ciliations"
    assert normalize("recon-\nciliations", "join").text == "reconciliations"
    assert normalize("a \u00ad\n b").text == "a b"

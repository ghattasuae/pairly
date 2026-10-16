# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind placeholder credit (security review 2026-10, L-05): a visible part of a quote
counts as evidence only when it holds one real word, a run of at least three letters or
digits, or is a complete number."""

from __future__ import annotations

import pytest

from pairly_core.extraction.evidence import _real_word


def _part(text: str, part: str) -> bool:
    lo = text.index(part)
    return _real_word(text, lo, lo + len(part))


@pytest.mark.parametrize(
    ("text", "part", "kept"),
    [
        ("BSc Computer Science, [university], 2019", "Science", True),
        ("Studied at [university], 2019", "2019", True),
        ("MSc, [university] of Dubai", "of", False),
        ("MSc, [university] of Dubai", "Dubai", True),
        ("Data Science, [university], 2019", "ce", False),
        # A number cut by the edge of the visible part is no number.
        ("[university], 2019", "20", False),
        ("[university], 2019", "19", False),
        # A complete number stays, with its separators.
        ("Led a team of 12 at [company]", "12", True),
        ("GPA 3.7 at [university]", "3.7", True),
        ("Cut costs 15% at [company]", "15%", True),
        ("Managed 1,200 accounts", "1,200", True),
        # Letters and digits of other scripts count like Latin ones.
        ("بكالوريوس، [university]", "بكالوريوس", True),
        ("في [university]", "في", False),
        ("x", "x", False),
        ("[university] A1", "A1", False),
    ],
)
def test_only_a_real_word_or_a_complete_number_is_evidence(
    text: str, part: str, kept: bool
) -> None:
    assert _part(text, part) is kept


def test_an_empty_part_is_never_evidence() -> None:
    assert _real_word("abc", 1, 1) is False

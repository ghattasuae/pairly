# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Persian sample words)
"""Zero-width padding next to Latin words is flagged; Arabic and Persian ZWNJ is not."""

from __future__ import annotations

import pytest

from pairly_core.documents.model import NormalizedDocument, Page, ParserInfo
from pairly_core.guardrails import Severity, sanitize_for_model, scan_document

ZWSP, ZWNJ, ZWJ, WJ = "\u200b", "\u200c", "\u200d", "\u2060"


def _zero_width(text: str) -> list[tuple[str, Severity]]:
    doc = NormalizedDocument(
        media_type="text/plain",
        parser=ParserInfo(name="test", version="1"),
        pages=[Page(number=1, width=0, height=0, unit="pt", text=text)],
    )
    report = scan_document(doc)
    return [(f.code, f.severity) for f in report.findings if f.code == "zero_width"]


@pytest.mark.parametrize(
    "text",
    [
        f"Skills: Python{ZWJ}{ZWJ}{ZWJ} Kubernetes{ZWJ}{ZWJ} AWS",
        f"Skills: Go{ZWNJ}{ZWNJ} SQL",
        f"Skills: {ZWSP}{ZWJ}Rust",
        f"Skills: Py{WJ}{ZWNJ}thon",
        f"Py{ZWJ}thon",
    ],
)
def test_padding_next_to_latin_is_medium(text: str) -> None:
    found = _zero_width(text)
    assert found
    assert all(sev is Severity.MEDIUM for _, sev in found)


@pytest.mark.parametrize(
    "text",
    [
        "می\u200cخواهم یک مهندس باشم",  # Persian: ZWNJ inside a word
        "خانه\u200cها و کتاب\u200cها",
        "زبان\u200c\u200cها",  # a doubled ZWNJ inside Persian is still Persian text
        "Team player \U0001f469\u200d\U0001f4bb",  # emoji sequence
    ],
)
def test_legitimate_joiners_are_left_alone(text: str) -> None:
    assert _zero_width(text) == []


def test_padding_is_removed_from_model_input() -> None:
    text = f"Skills: Python{ZWJ}{ZWJ}{ZWJ} AWS"
    doc = NormalizedDocument(
        media_type="text/plain",
        parser=ParserInfo(name="test", version="1"),
        pages=[Page(number=1, width=0, height=0, unit="pt", text=text)],
    )
    report = scan_document(doc)
    assert report.flagged
    assert ZWJ not in sanitize_for_model(doc, report).text

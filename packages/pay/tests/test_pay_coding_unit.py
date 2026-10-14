# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""GL coding suggestions without a database (``pairly_pay.coding``): similarity, the
vote and its confidence, corrections weighing more, list validation, the model answer
check, the auto-coding setting (off by default) and the committed recordings."""

from __future__ import annotations

import uuid

from pairly_pay import ai_recordings
from pairly_pay.checks.settings import parse_settings
from pairly_pay.coding import (
    EntryInput,
    Lists,
    ModelCoding,
    PastCoding,
    accept_model_answer,
    level_of,
    lines_from_values,
    similarity,
    suggest_from_history,
    validate_entries,
)
from pairly_pay.models import PayCodingEntry, PayCodingList

VENDOR = uuid.uuid4()
OTHER = uuid.uuid4()
GL = {"6100", "6200", "1500"}
CC = {"CC-ADM", "CC-IT"}


def past(
    description: str,
    gl: str | None,
    cc: str | None = None,
    *,
    vendor: uuid.UUID | None = VENDOR,
    source: str = "person",
    corrected: bool = False,
    reference: str = "AP-1",
) -> PastCoding:
    return PastCoding(uuid.uuid4(), reference, vendor, description, gl, cc, source, corrected)


def test_similarity_ignores_case_order_and_punctuation() -> None:
    assert similarity("Toner cartridge, black", "black TONER cartridge") == 1.0
    assert similarity("Toner cartridge, black", "Toner cartridge, colour") == 0.5
    assert similarity("", "Toner") == 0.0
    assert similarity("ورق طباعة", "ورق طباعة") == 1.0


def test_one_matching_line_is_medium_two_are_high() -> None:
    one = suggest_from_history(
        "Toner cartridge, black", VENDOR, [past("Toner cartridge, black", "6100", "CC-ADM")], GL, CC
    )
    assert one is not None
    assert (one.gl_code, one.cost_centre, one.level, one.method) == (
        "6100",
        "CC-ADM",
        "medium",
        "history",
    )
    two = suggest_from_history(
        "Toner cartridge, black",
        VENDOR,
        [past("Toner cartridge, black", "6100", "CC-ADM", reference=f"AP-{i}") for i in range(2)],
        GL,
        CC,
    )
    assert two is not None
    assert two.level == "high"
    assert two.examples == ("AP-0", "AP-1")


def test_other_vendors_and_weak_matches_count_less() -> None:
    other = suggest_from_history(
        "Toner cartridge, black",
        VENDOR,
        [past("Toner cartridge, black", "6100", vendor=OTHER) for _ in range(3)],
        GL,
        CC,
    )
    assert other is not None
    assert other.level == "medium"  # 0.7 of a same-vendor match
    unrelated = suggest_from_history(
        "Toner cartridge, black", VENDOR, [past("Office chair", "1500")], GL, CC
    )
    assert unrelated is None
    assert suggest_from_history(None, VENDOR, [past("Toner", "6100")], GL, CC) is None


def test_corrections_outweigh_and_auto_codes_weigh_less() -> None:
    history = [
        past("Whiteboard 120 x 90 cm", "6100"),
        past("Whiteboard 120 x 90 cm", "1500", corrected=True),
    ]
    found = suggest_from_history("Whiteboard 120 x 90 cm", VENDOR, history, GL, CC)
    assert found is not None
    assert found.gl_code == "1500"
    assert found.level == "low"  # the history disagrees
    auto = [past("Router rental", "6200", source="auto"), past("Router rental", "6100")]
    found = suggest_from_history("Router rental", VENDOR, auto, GL, CC)
    assert found is not None
    assert found.gl_code == "6100"


def test_codes_no_longer_in_force_never_vote() -> None:
    history = [past("Toner", "7777", "CC-OLD"), past("Toner", "7777", "CC-OLD")]
    assert suggest_from_history("Toner", VENDOR, history, GL, CC) is None


def test_levels() -> None:
    assert level_of(0.9) == "high"
    assert level_of(0.85) == "high"
    assert level_of(0.67) == "medium"
    assert level_of(0.59) == "low"


def test_list_validation_reports_every_problem() -> None:
    errors = validate_entries(
        [
            EntryInput("6100", "Office supplies"),
            EntryInput("6100", "Again"),
            EntryInput("bad code", "Space"),
            EntryInput("6200", "=HYPERLINK()"),
            EntryInput("6300", " "),
            EntryInput("6400", "x" * 161),
        ],
        rows=[2, 3, 4, 5, 6, 7],
    )
    codes = {(e.row, e.code) for e in errors}
    assert codes == {
        (3, "duplicate_code"),
        (4, "invalid_code"),
        (5, "formula_not_allowed"),
        (6, "required"),
        (7, "too_long"),
    }
    assert validate_entries([])[0].code == "required"


def _lists() -> Lists:
    gl = PayCodingList(id=uuid.uuid4(), kind="gl_account", version=1)
    cc = PayCodingList(id=uuid.uuid4(), kind="cost_centre", version=1)
    return Lists(
        gl,
        cc,
        (PayCodingEntry(code="6100", name="Office supplies"),),
        (PayCodingEntry(code="CC-ADM", name="Administration"),),
    )


def test_a_model_answer_must_name_codes_in_force() -> None:
    lists = _lists()
    found = accept_model_answer(ModelCoding(gl_code="6100", cost_centre="CC-ADM"), lists)
    assert found is not None
    assert (found.method, found.level, found.cost_centre) == ("model", "medium", "CC-ADM")
    assert accept_model_answer(ModelCoding(gl_code="9999"), lists) is None
    assert accept_model_answer(ModelCoding(gl_code=""), lists) is None
    unknown_cc = accept_model_answer(ModelCoding(gl_code="6100", cost_centre="CC-X"), lists)
    assert unknown_cc is not None
    assert unknown_cc.cost_centre is None


def test_lines_from_values() -> None:
    lines = lines_from_values(
        {
            "line_items[1].description": "Router rental",
            "line_items[0].description": " Fibre line ",
            "line_items[0].quantity": "1",
            "line_items[2].quantity": "2",
            "total": "10",
        }
    )
    assert [(x.index, x.description) for x in lines] == [
        (0, "Fibre line"),
        (1, "Router rental"),
        (2, None),
    ]


def test_auto_coding_is_off_by_default() -> None:
    assert parse_settings(None).auto_code_high_confidence is False
    assert parse_settings({"auto_code_high_confidence": "yes"}).auto_code_high_confidence is False
    assert parse_settings({"auto_code_high_confidence": True}).auto_code_high_confidence is True
    assert parse_settings({}).as_json()["auto_code_high_confidence"] is False


def test_recordings_are_current() -> None:
    assert ai_recordings.main(["--check"]) == 0

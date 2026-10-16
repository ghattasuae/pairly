# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""Recorded answers for rubric clarification suggestions (calibration mode), so the demo
works offline.

``uv run python -m pairly_hire.analytics.calibration_recordings`` writes
``pairly_hire/data/calibration_recordings.json``; ``--check`` writes nothing and fails
when the committed file is out of date (a test runs it). The ``hire-demo`` seed imports
it.

One entry per demo job criterion and per direction (people higher, lower, mixed). The
inputs are the criterion as written and the direction only (no candidate, no score), so
the recordings answer for any calibration of a demo job. Every scripted answer passes
``calibration.check_suggestion`` here, and the router validates it again on replay.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final, get_args

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_hire.analytics.calibration import (
    TASK,
    Direction,
    RubricClarification,
    check_suggestion,
    clarify_inputs,
)
from pairly_hire.demo import DEMO_JOBS
from pairly_hire.recordings import RecordingError, entry_for
from pairly_hire.rubric import Criterion

BUNDLE_PATH: Final = Path(__file__).resolve().parents[1] / "data" / "calibration_recordings.json"

LEADS: Final[dict[str, str]] = {
    "people_higher": (
        "People give more credit than the AI here, so the evidence description may leave "
        "out kinds of evidence people accept."
    ),
    "people_lower": (
        "People give less credit than the AI here, so the anchors may credit a mention "
        "that people do not count as evidence."
    ),
    "mixed": "People and the AI disagree in both directions here.",
}


def scripted(criterion: Criterion, direction: str) -> RubricClarification:
    return RubricClarification(
        suggestion=(
            f"{LEADS[direction]} For {criterion.name}, write in the anchors for 2 and 3 "
            "which concrete examples count, such as a named project or a stated result, "
            "and say that a skill listed without an example earns at most 1."
        )
    )


def build_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for job in DEMO_JOBS:
        for criterion in job.rubric.criteria:
            for direction in get_args(Direction):
                inputs = clarify_inputs(criterion, direction)
                answer = scripted(criterion, direction)
                problems = check_suggestion(answer.suggestion)
                if problems:
                    raise RecordingError(f"{criterion.id} ({direction}): {problems}")
                entries.append(entry_for(TASK, RubricClarification, inputs, answer))
    entries.sort(key=lambda e: e["request_hash"])
    return entries


def build_bundle() -> str:
    text = dumps_bundle(
        {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": build_entries()}
    )
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pairly_hire.analytics.calibration_recordings")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m pairly_hire.analytics.calibration_recordings): "
            "packages/hire/pairly_hire/data/calibration_recordings.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

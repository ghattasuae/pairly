# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recorded model responses for interview kits and debrief summaries (a recordings
bundle), so the demo works offline.

``uv run python -m pairly_hire.interviewing.recordings`` writes
``pairly_hire/data/interview_recordings.json``; ``--check`` writes nothing and fails when
the committed file is out of date (``make demo`` and a test run it). The ``hire-demo``
seed imports it.

Kits: one entry per demo job, per criterion of its default interview plan, per
screening result, per language. The inputs never carry candidate data, so they answer
for every candidate of a demo job. Every scripted answer passes ``kits.check_item``.

Debriefs: one entry per demo debrief (``interviewing.demo``), whose scorecards the demo
seed submits, in English and Arabic.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_hire.demo import DEMO_JOBS
from pairly_hire.interviewing import kits
from pairly_hire.interviewing.plan import criteria_by_id, default_plan
from pairly_hire.recordings import RecordingError, entry_for

BUNDLE_PATH: Final = Path(__file__).resolve().parents[1] / "data" / "interview_recordings.json"


def kit_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for job in DEMO_JOBS:
        plan = default_plan(job.rubric)
        known = criteria_by_id(job.rubric)
        for stage in plan.stages:
            for locale in ("en", "ar"):
                title = (
                    job.draft.title_ar if locale == "ar" and job.draft.title_ar else job.draft.title
                )
                for question in stage.questions:
                    criterion, _ = known[question.criterion_id]
                    text = (
                        question.question_ar
                        if locale == "ar" and question.question_ar
                        else question.question
                    )
                    for result in kits.RESULTS:
                        inputs = kits.kit_inputs(
                            job_title=title,
                            stage=kits.stage_name(stage, locale),
                            locale=locale,
                            criterion=criterion,
                            plan_question=text,
                            result=result,
                        )
                        answer = kits.scripted_item(inputs)
                        problems = kits.check_item(answer)
                        if problems:
                            raise RecordingError(
                                f"kit {job.draft.title} {criterion.id}: {problems}"
                            )
                        entries.append(entry_for(kits.TASK, kits.KitItemDraft, inputs, answer))
    return entries


def build_entries() -> list[dict[str, Any]]:
    from pairly_hire.interviewing.demo import debrief_entries

    entries = kit_entries() + debrief_entries()
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
    parser = argparse.ArgumentParser(prog="python -m pairly_hire.interviewing.recordings")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m pairly_hire.interviewing.recordings): "
            "packages/hire/pairly_hire/data/interview_recordings.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

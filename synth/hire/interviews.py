# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recorded responses for interview kits and the debrief summary of ``make demo``.

``make demo`` (``scripts/demo_seed.py``) opens two jobs with the synthetic backend
engineer rubric (``synth.hire.screening``). This bundle answers, offline:

* ``hire_interview_kit/v1`` for both jobs: every criterion of the default interview
  plan, every screening result, English and Arabic
  (``pairly_hire.interviewing.recordings.kit_entries_for``);
* ``hire_debrief_summary/v1`` for the demo debrief: the scorecard the demo seed submits
  for the interview of ``DEBRIEF_APPLICANT`` (``DEMO_SCORECARD``), English and Arabic.
  The scripted answer is the summary from the numbers (``debrief.template_draft``).

These are SCRIPTED fixtures, not model output. ``python -m synth.hire.interviews``
writes ``synth/hire/recordings/hire_interviews.json``; ``--check`` writes nothing and
fails when it is stale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_hire.interviewing import debrief
from pairly_hire.interviewing.kits import stage_name
from pairly_hire.interviewing.plan import criteria_by_id, default_plan
from pairly_hire.interviewing.recordings import kit_entries_for
from pairly_hire.recordings import RecordingError, entry_for
from pairly_hire.rubric import Rubric
from synth.hire.screening import DEMO_JOB, job_by_id, screening_rubric

BUNDLE_PATH: Final = Path(__file__).resolve().parent / "recordings" / "hire_interviews.json"
# The jobs of scripts/demo_seed.py (title, Arabic title).
DEMO_JOB_TITLES: Final = (("Backend Engineer", ""), ("Backend Engineer, Payments", ""))
# The applicant whose interview the demo seed scores (scripts/demo_seed.py), and its job.
DEBRIEF_APPLICANT: Final = "fair-01-base"
DEBRIEF_JOB_TITLE: Final = "Backend Engineer, Payments"
RATINGS: Final = (3, 4, 2, 3, 3)
DEMO_SCORECARD: Final[dict[str, Any]] = {
    "notes": (
        "Walked through a payment retry design with clear trade offs and real numbers. "
        "Less depth on query tuning: worth a follow-up in the next round."
    ),
    "recommendation": "yes",
}


def demo_rubric() -> Rubric:
    return screening_rubric(job_by_id(DEMO_JOB))


def demo_ratings(rubric: Rubric | None = None) -> dict[str, int]:
    """The demo interviewer's rating per criterion of the default plan."""
    (stage,) = default_plan(rubric or demo_rubric()).stages
    return {cid: RATINGS[i % len(RATINGS)] for i, cid in enumerate(stage.criteria())}


def debrief_entries(rubric: Rubric) -> list[dict[str, Any]]:
    plan = default_plan(rubric)
    (stage,) = plan.stages
    known = criteria_by_id(rubric)
    ratings = demo_ratings(rubric)
    entries: list[dict[str, Any]] = []
    for locale in ("en", "ar"):
        label = "المقابل 1" if locale == "ar" else "Interviewer 1"
        voice = debrief.Voice(
            label, label, DEMO_SCORECARD["recommendation"], DEMO_SCORECARD["notes"]
        )
        rated = [
            debrief.Rated(cid, known[cid][0].name, [(label, label, ratings[cid])])
            for cid in stage.criteria()
        ]
        inputs = debrief.debrief_inputs(
            job_title=DEBRIEF_JOB_TITLE,
            stage=stage_name(stage, locale),
            locale=locale,
            rated=rated,
            voices=[voice],
        )
        answer = debrief.template_draft(inputs)
        problems = debrief.check_draft(answer, inputs)
        if problems:
            raise RecordingError(f"debrief {locale}: {problems}")
        entries.append(entry_for(debrief.TASK, debrief.DebriefDraft, inputs, answer))
    return entries


def build_bundle() -> str:
    rubric = demo_rubric()
    entries: list[dict[str, Any]] = []
    for title, title_ar in DEMO_JOB_TITLES:
        entries += kit_entries_for(title, title_ar, rubric)
    entries += debrief_entries(rubric)
    entries.sort(key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)
    return text


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.hire.interviews")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m synth.hire.interviews): "
            "synth/hire/recordings/hire_interviews.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

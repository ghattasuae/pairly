# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Synthetic referees and the recorded reference summary of ``make demo``.

``scripts/demo_seed.py`` adds two answered references (``DEMO_REFERENCES``) to the
application whose debrief the demo shows (``synth.hire.interviews.DEBRIEF_APPLICANT``).
This bundle answers ``hire_reference_summary/v1`` for exactly those answers, English
and Arabic, offline. The scripted answer is the summary from the answers
(``references.summary.template_draft``).

These are SCRIPTED fixtures, not model output. ``python -m synth.hire.references``
writes ``synth/hire/recordings/hire_references.json``; ``--check`` writes nothing and
fails when it is stale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_hire.recordings import RecordingError, entry_for
from pairly_hire.references import summary as summaries
from synth.hire.interviews import DEBRIEF_JOB_TITLE

BUNDLE_PATH: Final = Path(__file__).resolve().parent / "recordings" / "hire_references.json"
DEMO_REFERENCES: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "Rania Khoury",
        "email": "rania.khoury@referee.example.test",
        "relationship": "manager",
        "answers": {
            "known_as": "manager",
            "company": "Falcon Payments",
            "candidate_title": "Backend Engineer",
            "worked_from": "2021-02",
            "worked_to": "2024-01",
            "strengths": "Owned the payment retry service end to end and explained trade offs "
            "clearly to the product team.",
            "development": "Could push back earlier when deadlines were unrealistic.",
            "reliability": 5,
            "would_rehire": "yes",
        },
    },
    {
        "name": "Sami Aziz",
        "email": "sami.aziz@referee.example.test",
        "relationship": "peer",
        "answers": {
            "known_as": "peer",
            "company": "Falcon Payments",
            "candidate_title": "Backend Engineer",
            "worked_from": "2022-05",
            "worked_to": "2024-01",
            "strengths": "Patient reviewer who left the codebase cleaner than they found it.",
            "development": "Query tuning on large tables was newer ground.",
            "reliability": 4,
            "would_rehire": "yes",
        },
    },
)


def demo_pairs(locale: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (summaries.label_for(n, locale), dict(ref["answers"]))
        for n, ref in enumerate(DEMO_REFERENCES, start=1)
    ]


def build_bundle() -> str:
    entries: list[dict[str, Any]] = []
    for locale in ("en", "ar"):
        inputs = summaries.summary_inputs(
            job_title=DEBRIEF_JOB_TITLE, locale=locale, references=demo_pairs(locale)
        )
        answer = summaries.template_draft(inputs)
        problems = summaries.check_draft(answer)
        if problems:
            raise RecordingError(f"reference summary {locale}: {problems}")
        entries.append(entry_for(summaries.TASK, summaries.ReferenceDraft, inputs, answer))
    entries.sort(key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)
    return text


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.hire.references")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m synth.hire.references): "
            "synth/hire/recordings/hire_references.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

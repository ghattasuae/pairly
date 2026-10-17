# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology

"""Recorded responses for AI message drafts (a recordings bundle), so the demo works
offline.

``uv run python -m pairly_hire.messaging.recordings`` writes
``pairly_hire/data/messaging_recordings.json``; ``--check`` writes nothing and fails
when the committed file is out of date (a test runs it). The ``hire-demo`` seed imports
it next to the drafting bundle.

One entry per demo job, per template with a personal paragraph
(``drafting.AI_TEMPLATES``), per language, without evidence (no rubric reason). The
inputs never name the candidate, so the recordings answer for every candidate of a demo
job. Every scripted answer passes ``drafting.check_draft`` here (the build fails
otherwise), and the router validates it again on replay.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_hire.demo import DEMO_JOBS
from pairly_hire.messaging import library
from pairly_hire.messaging.drafting import (
    AI_TEMPLATES,
    TASK,
    MessageDraft,
    check_draft,
    draft_inputs,
)
from pairly_hire.recordings import RecordingError, entry_for

BUNDLE_PATH: Final = Path(__file__).resolve().parents[1] / "data" / "messaging_recordings.json"

NOTES: Final[dict[str, dict[str, str]]] = {
    library.STATUS_UPDATE: {
        "en": "Thank you for your patience. Your application for the {job} role is still "
        "with our team, and we have not forgotten you.",
        "ar": "شكرا لصبرك. طلبك لوظيفة {job} ما زال لدى فريقنا، ولم ننسك.",
    },
    library.INTERVIEW_INVITE: {
        "en": "We enjoyed reading your application for the {job} role and look forward to "
        "talking with you.",
        "ar": "استمتعنا بقراءة طلبك لوظيفة {job} ونتطلع إلى الحديث معك.",
    },
    library.INTERVIEW_RESCHEDULE: {
        "en": "We are sorry for the change. We still look forward to talking with you about "
        "the {job} role.",
        "ar": "نعتذر عن هذا التغيير. ما زلنا نتطلع إلى الحديث معك عن وظيفة {job}.",
    },
    library.INTERVIEW_REMINDER: {
        "en": "We look forward to talking with you about the {job} role. Take your time to "
        "prepare, and ask us if anything is unclear.",
        "ar": "نتطلع إلى الحديث معك عن وظيفة {job}. خذ وقتك في الاستعداد، واسألنا إذا كان "
        "أي شيء غير واضح.",
    },
    library.REJECTION_SCREENING: {
        "en": "We know applying takes time and care, and we thank you for your interest in "
        "the {job} role.",
        "ar": "نعلم أن التقديم يتطلب وقتا وجهدا، ونشكرك على اهتمامك بوظيفة {job}.",
    },
    library.REJECTION_INTERVIEW: {
        "en": "Thank you for the open and thoughtful conversation about the {job} role. We "
        "appreciated your time.",
        "ar": "شكرا على الحوار الصريح والمدروس حول وظيفة {job}. نقدر وقتك.",
    },
    library.REJECTION_FINAL: {
        "en": "You were one of the last candidates for the {job} role, and the decision "
        "was not easy.",
        "ar": "كنت من آخر المرشحين لوظيفة {job}، ولم يكن القرار سهلا.",
    },
    library.WITHDRAWAL: {
        "en": "Thank you for letting us know. We wish you the very best with your next step.",
        "ar": "شكرا لإبلاغنا. نتمنى لك كل التوفيق في خطوتك القادمة.",
    },
}


def scripted(template_key: str, job_title: str, locale: str) -> MessageDraft:
    return MessageDraft(personal_note=NOTES[template_key][locale].format(job=job_title))


def demo_titles() -> list[tuple[str, str]]:
    """(locale, job title) pairs of the demo jobs."""
    pairs: list[tuple[str, str]] = []
    for job in DEMO_JOBS:
        pairs.append(("en", job.draft.title))
        pairs.append(("ar", job.draft.title_ar or job.draft.title))
    return pairs


def build_entries(titles: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for locale, title in titles if titles is not None else demo_titles():
        for key in AI_TEMPLATES:
            inputs = draft_inputs(key, title, locale)
            answer = scripted(key, title, locale)
            problems = check_draft(answer, inputs)
            if problems:
                raise RecordingError(f"{key} ({locale}, {title}): {problems}")
            entries.append(entry_for(TASK, MessageDraft, inputs, answer))
    entries.sort(key=lambda e: e["request_hash"])
    return entries


def bundle_text(entries: list[dict[str, Any]]) -> str:
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


def build_bundle() -> str:
    return bundle_text(build_entries())


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pairly_hire.messaging.recordings")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m pairly_hire.messaging.recordings): "
            "packages/hire/pairly_hire/data/messaging_recordings.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

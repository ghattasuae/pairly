# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic consistency flags: what referees said against the CV (brief 3.8.2).

The CV side is the screening profile (the work history read from the CV with its
evidence: employer, title, start and end month). Each flag names a referee by label,
never by name, and says what differs in a plain sentence (English and Arabic). Flags
are information for a person; nothing is decided from them.

* ``employer_not_on_cv``: the place the referee worked with the candidate matches no
  employer on the CV.
* ``title_differs``: the employer matches, but the title the referee gives shares no
  word with the CV's title there.
* ``dates_differ``: the employer matches, but the time worked together falls more than
  ``DATE_SLACK_MONTHS`` outside the CV's dates there.
* ``would_not_rehire`` and ``low_reliability`` (2 or lower of 5).
* ``referees_disagree``: reliability ratings of two referees three or more apart.
* ``no_cv_profile`` (information): there is no CV profile to compare with.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.service import read_details

DATE_SLACK_MONTHS: Final = 3
_STOP: Final = frozenset(
    {"the", "and", "of", "at", "for", "a", "an", "llc", "ltd", "inc", "fz", "fzco", "fze"}
)
_WORD_RE: Final = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class CvJob:
    employer: str
    title: str
    start: str | None
    end: str | None


@dataclass(frozen=True, slots=True)
class Flag:
    kind: str
    referee: str  # label, e.g. "Referee 1"
    detail: str
    detail_ar: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "referee": self.referee,
            "detail": self.detail,
            "detail_ar": self.detail_ar,
        }


def _words(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return {w for w in _WORD_RE.findall(folded) if w not in _STOP and len(w) > 1}


def same_employer(a: str, b: str) -> bool:
    left, right = _words(a), _words(b)
    if not left or not right:
        return False
    return left <= right or right <= left or len(left & right) >= 2


def _month(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        return None
    return year * 12 + month


def _value(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("value") or "").strip()
    return str(item or "").strip()


async def cv_jobs(session: AsyncSession, application_id: uuid.UUID) -> list[CvJob] | None:
    """The CV's work history from the latest screening, or ``None`` without a profile."""
    row = await session.scalar(
        select(HireScreening)
        .where(HireScreening.application_id == application_id)
        .order_by(HireScreening.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    profile = read_details(row).get("profile")
    if not isinstance(profile, dict) or not profile.get("parsed", True):
        return None
    jobs: list[CvJob] = []
    for entry in profile.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        employer = _value(entry.get("employer"))
        if not employer:
            continue
        jobs.append(
            CvJob(
                employer=employer,
                title=_value(entry.get("title")),
                start=entry.get("start") if isinstance(entry.get("start"), str) else None,
                end=entry.get("end") if isinstance(entry.get("end"), str) else None,
            )
        )
    return jobs


def flags_for(references: list[tuple[str, dict[str, Any]]], jobs: list[CvJob] | None) -> list[Flag]:
    """``references``: (label, submitted answers) in order."""
    flags: list[Flag] = []
    if jobs is None:
        flags.append(
            Flag(
                "no_cv_profile",
                "",
                "There is no CV profile to compare the references with.",
                "لا يوجد ملف سيرة ذاتية لمقارنة المراجع به.",
            )
        )
    for label, answers in references:
        company = str(answers.get("company") or "")
        title = str(answers.get("candidate_title") or "")
        if jobs is not None and company:
            matches = [j for j in jobs if same_employer(company, j.employer)]
            if not matches:
                flags.append(
                    Flag(
                        "employer_not_on_cv",
                        label,
                        f"{label} worked with the candidate at {company}, which is not on the CV.",
                        f"عمل {label} مع المرشح في {company}، وهي غير مذكورة في السيرة الذاتية.",
                    )
                )
            else:
                if title and not any(_words(title) & _words(j.title) for j in matches if j.title):
                    cv_titles = ", ".join(sorted({j.title for j in matches if j.title}))
                    if cv_titles:
                        flags.append(
                            Flag(
                                "title_differs",
                                label,
                                f"{label} gives the title {title}; the CV says {cv_titles}.",
                                f"يذكر {label} المسمى {title}؛ وتذكر السيرة الذاتية {cv_titles}.",
                            )
                        )
                start = _month(answers.get("worked_from"))
                end = _month(answers.get("worked_to"))
                if start is not None and not any(_within(start, end, j) for j in matches):
                    flags.append(
                        Flag(
                            "dates_differ",
                            label,
                            f"The dates {label} gives for {company} do not match the CV.",
                            f"التواريخ التي يذكرها {label} عن {company} لا تطابق السيرة الذاتية.",
                        )
                    )
        if answers.get("would_rehire") == "no":
            flags.append(
                Flag(
                    "would_not_rehire",
                    label,
                    f"{label} would not work with the candidate again.",
                    f"لن يعمل {label} مع المرشح مرة أخرى.",
                )
            )
        reliability = answers.get("reliability")
        if isinstance(reliability, int) and reliability <= 2:
            flags.append(
                Flag(
                    "low_reliability",
                    label,
                    f"{label} rates reliability {reliability} of 5.",
                    f"يقيم {label} الالتزام بـ {reliability} من 5.",
                )
            )
    ratings = [
        (label, a["reliability"])
        for label, a in references
        if isinstance(a.get("reliability"), int)
    ]
    if ratings:
        low = min(ratings, key=lambda r: r[1])
        high = max(ratings, key=lambda r: r[1])
        if high[1] - low[1] >= 3:
            flags.append(
                Flag(
                    "referees_disagree",
                    "",
                    f"Reliability ratings range from {low[1]} ({low[0]}) to {high[1]} ({high[0]}).",
                    f"تتراوح تقييمات الالتزام من {low[1]} ({low[0]}) إلى {high[1]} ({high[0]}).",
                )
            )
    return flags


def _within(start: int, end: int | None, job: CvJob) -> bool:
    cv_start = _month(job.start)
    cv_end = _month(job.end)
    if cv_start is None:
        return True  # nothing to compare with
    if start < cv_start - DATE_SLACK_MONTHS:
        return False
    # "Still working together" against a job the CV says ended is a mismatch too.
    return cv_end is None or (
        end is not None
        and end <= cv_end + DATE_SLACK_MONTHS
        and start <= cv_end + DATE_SLACK_MONTHS
    )

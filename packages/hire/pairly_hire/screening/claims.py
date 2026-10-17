# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Claim consistency checks and AI-generated application signals (brief 3.3.6, 3.3.7).

Deterministic, over the parsed profile and the sanitised CV text. Each check gives
flags for a person to look at, with a one-sentence explanation. A flag is information:
it never changes a score, never rejects, never moves an application.

Claim consistency (``kind="claim"``):

* ``end_before_start``: a job ends before it starts.
* ``future_date``: a job starts after the screening date.
* ``overlapping_roles``: two jobs overlap by more than ``overlap_months``.
* ``unexplained_gap``: more than ``gap_months`` between two jobs with nothing listed.
* ``title_inflation``: a senior title held after less experience than the curated
  table (``TITLE_LEVELS``) expects for it.
* ``unsupported_skills``: at least ``unsupported_skills_min`` listed skills that no
  other part of the CV (titles, achievements, summary, certifications) mentions.

AI-generated application signals (``kind="ai_signal"``):

* ``boilerplate``: many generic phrases and few specifics (numbers, named tools).
* ``mass_application``: the same CV file (same SHA-256) sent to several jobs of the
  organisation.

Employer existence checks (brief 3.3.6, "employers that cannot be matched to any public
presence") need an external source and are not done here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from pairly_hire.screening.profile import Job, ParsedProfile


@dataclass(frozen=True, slots=True)
class ClaimConfig:
    gap_months: int = 6
    overlap_months: int = 3
    # Tuned on the synthetic corpus: clean CVs list up to 9 skills no other line uses.
    unsupported_skills_min: int = 12
    mass_application_jobs: int = 3


@dataclass(frozen=True, slots=True)
class Flag:
    code: str
    kind: Literal["claim", "ai_signal"]
    explanation: str
    # Where to look: indexes into the profile's experience, skill names, counts.
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "severity": "info",
            "explanation": self.explanation,
            "details": self.details,
        }


# ------------------------------------------------------------------ dates


def month_bounds(value: str | None) -> tuple[int, int] | None:
    """The earliest and latest month a ``YYYY-MM`` or ``YYYY`` value can mean (a year
    alone can be any of its twelve months), as month counts."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", value)
    if match is None:
        return None
    year = int(match.group(1))
    if match.group(2):
        month = year * 12 + int(match.group(2)) - 1
        return month, month
    return year * 12, year * 12 + 11


@dataclass(frozen=True, slots=True)
class _Period:
    """A job's dates as ranges, so a check only flags what is certain whatever month a
    year-only date means."""

    index: int
    job: Job
    start_lo: int
    start_hi: int
    end_lo: int
    end_hi: int  # inclusive month counts
    current: bool


def _periods(profile: ParsedProfile, today: int) -> list[_Period]:
    out: list[_Period] = []
    for i, job in enumerate(profile.experience):
        start = month_bounds(job.start)
        if start is None:
            continue
        end = month_bounds(job.end) or (today, today)
        out.append(_Period(i, job, *start, *end, current=job.end is None))
    return out


def _months(n: int) -> str:
    return "1 month" if n == 1 else f"{n} months"


def _title(job: Job) -> str:
    return job.title.value if job.title else "a job"


def date_checks(profile: ParsedProfile, as_of: date, config: ClaimConfig) -> list[Flag]:
    today = as_of.year * 12 + as_of.month - 1
    periods = _periods(profile, today)
    flags: list[Flag] = []
    for p in periods:
        if p.start_lo > today:
            flags.append(
                Flag(
                    "future_date",
                    "claim",
                    f"The CV says {_title(p.job)} starts after the screening date.",
                    {"experience": [p.index]},
                )
            )
        if not p.current and p.end_hi < p.start_lo:
            flags.append(
                Flag(
                    "end_before_start",
                    "claim",
                    f"The CV says {_title(p.job)} ends before it starts.",
                    {"experience": [p.index]},
                )
            )
    valid = sorted(
        (p for p in periods if p.end_hi >= p.start_lo), key=lambda p: (p.start_lo, p.end_hi)
    )
    for i, a in enumerate(valid):
        for b in valid[i + 1 :]:
            # Months both jobs certainly share.
            overlap = min(a.end_lo, b.end_lo) - max(a.start_hi, b.start_hi) + 1
            if overlap > config.overlap_months:
                flags.append(
                    Flag(
                        "overlapping_roles",
                        "claim",
                        f"Two jobs overlap by {_months(overlap)}: "
                        f"{_title(a.job)} and {_title(b.job)}.",
                        {"experience": sorted([a.index, b.index]), "months": overlap},
                    )
                )
    covered_until: _Period | None = None
    for p in valid:
        if covered_until is not None:
            # Months certainly without a job between the two.
            gap = p.start_lo - covered_until.end_hi - 1
            if gap > config.gap_months:
                flags.append(
                    Flag(
                        "unexplained_gap",
                        "claim",
                        f"There are {_months(gap)} between {_title(covered_until.job)} and "
                        f"{_title(p.job)} with nothing listed.",
                        {"experience": [covered_until.index, p.index], "months": gap},
                    )
                )
        if covered_until is None or p.end_hi > covered_until.end_hi:
            covered_until = p
    return flags


# ------------------------------------------------------------------ titles

# Curated: a title pattern and the years of earlier work a person usually has before
# holding it. Conservative on purpose: a flag asks a person to look, nothing more.
TITLE_LEVELS: Final[tuple[tuple[re.Pattern[str], int, str], ...]] = (
    (
        re.compile(
            r"\bchief\b|\bc[etfo]o\b|\bvice\s+president\b|\bvp\b|"
            r"الرئيس\s+التنفيذي|نائب\s+الرئيس",
            re.IGNORECASE,
        ),
        8,
        "a chief or vice president title",
    ),
    (
        re.compile(r"\bdirector\b|\bhead\s+of\b|\bgeneral\s+manager\b", re.IGNORECASE),
        6,
        "a director or head of title",
    ),
    (re.compile(r"\bprincipal\b|\bstaff\s+engineer\b", re.IGNORECASE), 5, "a principal title"),
)


def title_checks(profile: ParsedProfile, as_of: date) -> list[Flag]:
    today = as_of.year * 12 + as_of.month - 1
    periods = [p for p in _periods(profile, today) if p.end_hi >= p.start_lo]
    flags: list[Flag] = []
    for p in periods:
        title = _title(p.job)
        for pattern, years, label in TITLE_LEVELS:
            if not pattern.search(title):
                continue
            # The most earlier work the dates allow: flag only when even that is short.
            before = sum(
                max(0, min(q.end_hi, p.start_hi - 1) - q.start_lo + 1)
                for q in periods
                if q.index != p.index and q.start_lo < p.start_hi
            )
            if before < years * 12:
                flags.append(
                    Flag(
                        "title_inflation",
                        "claim",
                        f"{title} is {label}, held after about {before // 12} years of "
                        f"listed work; such titles usually follow {years} or more.",
                        {"experience": [p.index], "months_before": before},
                    )
                )
            break
    return flags


# ------------------------------------------------------------------ skills


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w+#.]+", " ", text.casefold()).split())


def skill_checks(
    profile: ParsedProfile, text: str, exclude: Sequence[tuple[int, int]], config: ClaimConfig
) -> list[Flag]:
    """Skills that appear nowhere in ``text`` outside the ``exclude`` ranges (the skill
    list itself, and text the guardrails removed)."""
    if not profile.skills:
        return []
    keep: list[str] = []
    position = 0
    for start, end in sorted(exclude):
        keep.append(text[position:start])
        position = max(position, end)
    keep.append(text[position:])
    rest = f" {_norm(' '.join(keep))} "
    unsupported = [
        s.value for s in profile.skills if _norm(s.value) and f" {_norm(s.value)} " not in rest
    ]
    if len(unsupported) < config.unsupported_skills_min:
        return []
    return [
        Flag(
            "unsupported_skills",
            "claim",
            f"{len(unsupported)} listed skills appear nowhere else in the CV.",
            {"skills": unsupported[:30]},
        )
    ]


# ------------------------------------------------------------------ AI-generated signals

BOILERPLATE: Final = (
    "results-driven",
    "results driven",
    "detail-oriented",
    "detail oriented",
    "team player",
    "self-motivated",
    "self motivated",
    "hard-working",
    "hardworking",
    "proven track record",
    "dynamic",
    "passionate",
    "synergy",
    "synergies",
    "go-getter",
    "think outside the box",
    "fast-paced environment",
    "highly motivated",
    "excellent communication skills",
    "leverage",
    "leveraging",
    "best-in-class",
    "cutting-edge",
    "spearheaded",
    "seamless",
    "innovative",
    "strategic thinker",
    "value-added",
)


def boilerplate_signal(text: str, min_phrases: int = 5) -> list[Flag]:
    folded = text.casefold()
    found = sorted({p for p in BOILERPLATE if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", folded)})
    words = max(1, len(folded.split()))
    numbers = len(re.findall(r"\d+(?:[.,]\d+)?%?", text))
    if len(found) < min_phrases or numbers / words > 0.02:
        return []
    return [
        Flag(
            "boilerplate",
            "ai_signal",
            f"The CV uses {len(found)} generic phrases and few specific numbers.",
            {"phrases": found[:20], "numbers": numbers},
        )
    ]


def mass_application_signal(jobs_with_same_cv: int, config: ClaimConfig) -> list[Flag]:
    if jobs_with_same_cv < config.mass_application_jobs:
        return []
    return [
        Flag(
            "mass_application",
            "ai_signal",
            f"The same CV file was sent to {jobs_with_same_cv} jobs here.",
            {"jobs": jobs_with_same_cv},
        )
    ]


def all_checks(
    profile: ParsedProfile | None,
    text: str,
    exclude: Sequence[tuple[int, int]],
    *,
    as_of: date,
    jobs_with_same_cv: int,
    config: ClaimConfig,
) -> list[Flag]:
    """Every flag for one application, claims first. ``text`` is the original CV text;
    ``exclude`` the ranges of it the skill check ignores (see ``skill_checks``)."""
    flags: list[Flag] = []
    if profile is not None and profile.parsed:
        flags += date_checks(profile, as_of, config)
        flags += title_checks(profile, as_of)
        flags += skill_checks(profile, text, exclude, config)
    flags += boilerplate_signal(text)
    flags += mass_application_signal(jobs_with_same_cv, config)
    return flags

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""Requirements sanity check (brief 3.1.2): requirements nobody can meet. Deterministic.

Rules:

* ``sanity.years_exceed_age``: more years of a technology than it has existed
  ("10 years of Kubernetes" when Kubernetes was first released in 2014). Uses
  ``TECH_FIRST_RELEASE``, a small curated table of first public release years.
* ``sanity.seniority_conflict``: a junior or entry level title with many years required,
  a senior title with almost none, or junior and senior in the same title.
* ``sanity.years_unrealistic``: more than 25 years of anything.

English and Arabic ("5 سنوات خبرة في Kubernetes", Arabic-Indic digits included). The
checks use the year passed in (``today``), so tests and replays are stable.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final

from pairly_hire.language import Finding

# First public release year. Curated from each project's own release history; extend
# with care (the earliest public release, not the first stable major).
TECH_FIRST_RELEASE: Final[dict[str, tuple[str, int]]] = {
    "kubernetes": ("Kubernetes", 2014),
    "k8s": ("Kubernetes", 2014),
    "docker": ("Docker", 2013),
    "react": ("React", 2013),
    "react native": ("React Native", 2015),
    "vue": ("Vue.js", 2014),
    "vue.js": ("Vue.js", 2014),
    "angular": ("Angular", 2016),
    "svelte": ("Svelte", 2016),
    "next.js": ("Next.js", 2016),
    "nextjs": ("Next.js", 2016),
    "typescript": ("TypeScript", 2012),
    "node.js": ("Node.js", 2009),
    "nodejs": ("Node.js", 2009),
    "deno": ("Deno", 2018),
    "bun": ("Bun", 2022),
    "go": ("Go", 2009),
    "golang": ("Go", 2009),
    "rust": ("Rust", 2010),
    "kotlin": ("Kotlin", 2011),
    "swift": ("Swift", 2014),
    "swiftui": ("SwiftUI", 2019),
    "jetpack compose": ("Jetpack Compose", 2021),
    "flutter": ("Flutter", 2017),
    "dart": ("Dart", 2011),
    "terraform": ("Terraform", 2014),
    "ansible": ("Ansible", 2012),
    "graphql": ("GraphQL", 2015),
    "fastapi": ("FastAPI", 2018),
    "tailwind": ("Tailwind CSS", 2017),
    "tailwind css": ("Tailwind CSS", 2017),
    "pytorch": ("PyTorch", 2016),
    "tensorflow": ("TensorFlow", 2015),
    "langchain": ("LangChain", 2022),
    "langgraph": ("LangGraph", 2024),
    "kafka": ("Apache Kafka", 2011),
    "spark": ("Apache Spark", 2014),
    "snowflake": ("Snowflake", 2014),
    "dbt": ("dbt", 2016),
    "airflow": ("Apache Airflow", 2015),
    "elasticsearch": ("Elasticsearch", 2010),
    "mongodb": ("MongoDB", 2009),
    "redis": ("Redis", 2009),
    "aws": ("AWS", 2006),
    "azure": ("Microsoft Azure", 2010),
    "gcp": ("Google Cloud", 2008),
    "google cloud": ("Google Cloud", 2008),
    ".net core": (".NET Core", 2016),
    "github actions": ("GitHub Actions", 2019),
    "power bi": ("Power BI", 2015),
    "figma": ("Figma", 2016),
    "openai api": ("OpenAI API", 2020),
    "chatgpt": ("ChatGPT", 2022),
}

MAX_REALISTIC_YEARS: Final = 25

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TECH_ALT = "|".join(re.escape(k) for k in sorted(TECH_FIRST_RELEASE, key=lambda k: (-len(k), k)))
# "10+ years of Kubernetes", "10 years experience with React", "5 yrs in Go".
_EN_YEARS = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:'|’)?\s+(?:of\s+)?(?:(?:hands-on|professional|"
    r"commercial|production|relevant)\s+)?(?:experience\s+)?(?:(?:with|in|using|of|on)\s+)?"
    r"(?P<tech>" + _TECH_ALT + r")(?![\w+#-]|\.\w)",
    re.IGNORECASE,
)
# "Kubernetes (10+ years)", "React: 8 years".
_EN_YEARS_AFTER = re.compile(
    r"(?<![\w.])(?P<tech>" + _TECH_ALT + r")\s*(?:\(|:|,|-)\s*(?P<n>\d{1,2})\s*\+?\s*"
    r"(?:years?|yrs?)",
    re.IGNORECASE,
)
# "خبرة 10 سنوات في Kubernetes", "10 سنوات خبرة في React".
_AR_YEARS = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*(?:سنوات|سنة|عاما|أعوام)\s+(?:من\s+)?(?:ال)?(?:خبرة\s+)?"
    r"(?:(?:في|ب|على|مع)\s*)?(?P<tech>" + _TECH_ALT + r")(?![\w+#-]|\.\w)",
    re.IGNORECASE,
)
_ANY_YEARS = re.compile(
    r"(?P<n>\d{1,2})\s*\+?\s*(?:years?|yrs?|سنوات|سنة|عاما|أعوام)", re.IGNORECASE
)

_JUNIOR = re.compile(
    r"\b(junior|entry[- ]level|graduate|intern(ship)?|trainee|associate)\b|"
    r"(?<![\u0621-\u064A])(مبتدئ|متدرب|حديث التخرج)(?![\u0621-\u064A])",
    re.IGNORECASE,
)
_SENIOR = re.compile(
    r"\b(senior|lead|principal|head of|director|architect)\b|"
    r"(?<![\u0621-\u064A])(أول|أولى|خبير|رئيس قسم)(?![\u0621-\u064A])",
    re.IGNORECASE,
)

JUNIOR_MAX_YEARS: Final = 3
SENIOR_MIN_YEARS: Final = 2


def _years(text: str) -> list[tuple[int, int, int, str]]:
    """``(years, start, end, match)`` of every years-of-experience mention."""
    found: list[tuple[int, int, int, str]] = []
    for match in _ANY_YEARS.finditer(text):
        found.append((int(match.group("n")), match.start(), match.end(), match.group(0)))
    return found


def check_sanity(text: str, *, title: str = "", today: date | None = None) -> list[Finding]:
    """Findings for a job's requirements ``text`` (and its ``title``), by position."""
    # Arabic-Indic digits become ASCII; same length, so offsets stay valid.
    body = text.translate(_ARABIC_DIGITS)
    title_text = title.translate(_ARABIC_DIGITS)
    year = (today or date.today()).year
    findings: list[Finding] = []
    seen: set[tuple[int, int]] = set()

    for pattern in (_EN_YEARS, _EN_YEARS_AFTER, _AR_YEARS):
        for match in pattern.finditer(body):
            years = int(match.group("n"))
            key = match.group("tech").lower()
            name, released = TECH_FIRST_RELEASE[key]
            possible = max(year - released, 0)
            span = (match.start(), match.end())
            if years > possible and span not in seen:
                seen.add(span)
                findings.append(
                    Finding(
                        rule="sanity.years_exceed_age",
                        category="sanity",
                        severity="warning",
                        start=match.start(),
                        end=match.end(),
                        match=text[match.start() : match.end()],
                        message=(
                            f"{name} was first released in {released}, so nobody can have "
                            f"more than {possible} years of it."
                        ),
                        suggestions=(f"{min(possible, max(possible - 2, 1))} years of {name}",),
                    )
                )

    mentions = _years(body)
    for years, start, end, _raw in mentions:
        if years > MAX_REALISTIC_YEARS:
            findings.append(
                Finding(
                    rule="sanity.years_unrealistic",
                    category="sanity",
                    severity="warning",
                    start=start,
                    end=end,
                    match=text[start:end],
                    message=f"{years} years is more than almost anyone has. Check the number.",
                    suggestions=("10+ years",),
                )
            )

    junior = _JUNIOR.search(title_text)
    senior = _SENIOR.search(title_text)
    most = max((m[0] for m in mentions), default=None)
    if junior and senior:
        findings.append(
            _seniority(
                title_text,
                junior.start(),
                junior.end(),
                "The title says both junior and senior. Pick one level.",
            )
        )
    elif junior and most is not None and most > JUNIOR_MAX_YEARS:
        findings.append(
            _seniority(
                title_text,
                junior.start(),
                junior.end(),
                f"A {junior.group(0).lower()} role asking for {most} years of experience is "
                "a contradiction. Lower the years or raise the level.",
            )
        )
    elif senior and most is not None and most < SENIOR_MIN_YEARS:
        findings.append(
            _seniority(
                title_text,
                senior.start(),
                senior.end(),
                f"A {senior.group(0).lower()} role asking for {most} year(s) of experience is "
                "unusual. Check the level or the years.",
            )
        )
    findings.sort(key=lambda f: (f.category != "seniority", f.start, f.rule))
    return findings


def _seniority(title: str, start: int, end: int, message: str) -> Finding:
    """A finding on the title (offsets into the title, category ``seniority``)."""
    return Finding(
        rule="sanity.seniority_conflict",
        category="seniority",
        severity="warning",
        start=start,
        end=end,
        match=title[start:end],
        message=message,
        suggestions=(),
    )

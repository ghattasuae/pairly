# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic test text)
"""The inclusive-language checker and the requirements sanity check, English and Arabic."""

from __future__ import annotations

from datetime import date

import pytest

from pairly_hire.language.inclusive import check_inclusive
from pairly_hire.language.sanity import TECH_FIRST_RELEASE, check_sanity

TODAY = date(2026, 10, 8)


def rules(text: str, lang: str | None = None) -> set[str]:
    return {f.rule for f in check_inclusive(text, lang=lang)}


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("We need a salesman for our Dubai office.", "en.gendered.salesman"),
        ("Join our manpower planning team.", "en.gendered.manpower"),
        ("Hey guys, we are hiring!", "en.gendered.guys"),
        ("He will manage the accounts.", "en.gendered.pronoun"),
        ("Male candidates only.", "en.gendered.only"),
        ("We want an aggressive closer.", "en.gendered.coded"),
        ("Looking for a young and dynamic person.", "en.age.young"),
        ("Ideal for digital natives.", "en.age.digital_native"),
        ("Recent graduates welcome.", "en.age.recent_graduate"),
        ("Age: under 35.", "en.age.limit"),
        ("Candidates between 25 and 35 years old.", "en.age.limit"),
        ("No more than 5 years experience.", "en.age.max_years"),
        ("Native English speaker required.", "en.origin.native"),
        ("Emirati nationals only.", "en.origin.nationality"),
        ("Please attach a recent photo.", "en.personal.photo"),
        ("Single candidates preferred.", "en.personal.marital"),
        ("Please state your religion.", "en.personal.religion"),
        ("A crazy fast-paced team.", "en.ability.crazy"),
        ("Must be able-bodied.", "en.ability.able_bodied"),
        ("Hiring a coding rockstar.", "en.jargon.rockstar"),
        ("We hire for culture fit.", "en.jargon.culture_fit"),
        ("Work hard, play hard.", "en.jargon.work_hard_play_hard"),
    ],
)
def test_english_findings(text: str, rule: str) -> None:
    assert rule in rules(text, "en")


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("مطلوب محاسب للعمل في دبي", "ar.gendered.title"),
        ("الوظيفة للذكور فقط", "ar.gendered.only"),
        ("نبحث عن شباب طموحين", "ar.age.young"),
        ("على ألا يزيد عمره عن 30 سنة", "ar.age.limit"),
        ("العمر من 25 إلى 35", "ar.age.limit"),
        ("نرحب بحديثي التخرج", "ar.age.recent_graduate"),
        ("متحدث أصلي للغة الإنجليزية", "ar.origin.native"),
        ("للمواطنين فقط", "ar.origin.nationality"),
        ("يرجى إرفاق صورة شخصية", "ar.personal.photo"),
        ("يفضل أن يكون أعزب", "ar.personal.marital"),
        ("يذكر المتقدم الديانة", "ar.personal.religion"),
        ("أن يكون لائقا طبيا", "ar.ability.fit"),
        ("نبحث عن نينجا برمجة", "ar.jargon.rockstar"),
    ],
)
def test_arabic_findings(text: str, rule: str) -> None:
    assert rule in rules(text, "ar")


@pytest.mark.parametrize(
    "text",
    [
        "You will build Python services with a friendly team.",
        "5+ years of experience with PostgreSQL.",
        "Over 10 years of experience in finance.",
        "Fluent in English and Arabic.",
        "You will lead a team of eight.",
    ],
)
def test_clean_english_text_has_no_findings(text: str) -> None:
    assert check_inclusive(text, lang="en") == []


@pytest.mark.parametrize(
    "text",
    [
        "مطلوب مهندس/ة برمجيات",
        "مطلوب محاسب (ذكر/أنثى)",
        "يجيد اللغة العربية والإنجليزية",
        "خبرة 5 سنوات في الحسابات",
    ],
)
def test_clean_arabic_text_has_no_findings(text: str) -> None:
    assert check_inclusive(text, lang="ar") == []


def test_findings_carry_offsets_and_suggestions() -> None:
    text = "We need a salesman."
    (finding,) = check_inclusive(text, lang="en")
    assert text[finding.start : finding.end] == "salesman"
    assert "salesperson" in finding.suggestions
    assert finding.message
    assert "—" not in finding.message
    assert finding.as_dict()["rule"] == "en.gendered.salesman"


def test_both_languages_by_default() -> None:
    found = rules("Male candidates only. الوظيفة للذكور فقط")
    assert {"en.gendered.only", "ar.gendered.only"} <= found


# ------------------------------------------------------------------ sanity


def sanity(text: str, title: str = "Engineer") -> list[tuple[str, str]]:
    return [(f.rule, f.match) for f in check_sanity(text, title=title, today=TODAY)]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("We want 10 years of LangChain.", "10 years of LangChain"),
        ("Must have 15+ years experience with Kubernetes.", "15+ years experience with Kubernetes"),
        ("Requires 20 yrs in Rust", "20 yrs in Rust"),
        ("SwiftUI: 9 years", "SwiftUI: 9 years"),
        ("Bun (6 years)", "Bun (6 years"),
        ("خبرة 15 سنوات في Kubernetes", "15 سنوات في Kubernetes"),
        ("١٥ سنة خبرة في React", "١٥ سنة خبرة في React"),
    ],
)
def test_years_exceed_technology_age(text: str, match: str) -> None:
    found = sanity(text)
    assert ("sanity.years_exceed_age", match) in found


@pytest.mark.parametrize(
    "text",
    [
        "5 years of Kubernetes.",
        "12 years of Kubernetes.",
        "3 years with React Native and 10 years of Python.",
        "8 years experience in Go",
    ],
)
def test_possible_years_pass(text: str) -> None:
    assert not [r for r, _ in sanity(text) if r == "sanity.years_exceed_age"]


def test_message_names_the_release_year() -> None:
    (finding,) = check_sanity("10 years of LangChain", today=TODAY)
    assert "2022" in finding.message
    assert "4 years" in finding.message


def test_unrealistic_years() -> None:
    assert ("sanity.years_unrealistic", "30 years") in sanity("At least 30 years in sales.")


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("Junior Data Analyst", "At least 8 years of experience."),
        ("Graduate Engineer", "5+ years in industry."),
        ("Senior Engineer", "1 year of experience."),
        ("Junior Senior Developer", "2 years."),
        ("محاسب مبتدئ", "خبرة 7 سنوات"),
    ],
)
def test_seniority_conflicts(title: str, text: str) -> None:
    assert "sanity.seniority_conflict" in {r for r, _ in sanity(text, title)}


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("Junior Data Analyst", "1 to 2 years of experience."),
        ("Senior Engineer", "6 years of experience."),
        ("Customer Support Lead", "Experience leading a team."),
    ],
)
def test_consistent_seniority_passes(title: str, text: str) -> None:
    assert "sanity.seniority_conflict" not in {r for r, _ in sanity(text, title)}


def test_table_is_curated_and_plausible() -> None:
    for key, (name, year) in TECH_FIRST_RELEASE.items():
        assert key == key.lower()
        assert name
        assert 1990 <= year <= TODAY.year

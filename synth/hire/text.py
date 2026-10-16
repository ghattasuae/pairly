# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The text of a CV, line by line, in English, Arabic or both, plus its ground truth.

``build_text(facts, language, date_style)`` returns ``CvText``: every line a renderer
draws (in logical order), each tagged with its section and a reference to the fact it
shows (``exp:0``, ``exp:0:h:1``, ``edu:0``, ``cert:0``, ``skill:3``, ``langs``). The
labeler quotes these lines as evidence, so evidence is always text that is on the page.
Renderers may wrap a long line; quote matching ignores whitespace differences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from synth.hire.facts import AS_OF, CvFacts, Job
from synth.hire.models import (
    Certification,
    Contact,
    CvGroundTruth,
    Education,
    ExperienceEntry,
    LanguageSkill,
    Link,
)
from synth.hire.vocab import (
    CITIES,
    DEGREES,
    FAMILIES,
    FIELDS_AR,
    HEADINGS,
    LANGUAGES,
    LEVELS_LANG,
    MONTHS_AR,
    MONTHS_EN,
)

Language = Literal["en", "ar", "bilingual"]
DateStyle = Literal["mon_year", "numeric", "iso", "year"]
LineKind = Literal[
    "name", "alt_name", "headline", "contact", "heading", "entry", "dates", "bullet", "text"
]

SKILLS_AR: Final[dict[str, str]] = {
    "unit testing": "اختبارات الوحدات",
    "three-way matching": "المطابقة الثلاثية",
    "invoice processing": "معالجة الفواتير",
    "VAT": "ضريبة القيمة المضافة",
    "vendor reconciliation": "مطابقة حسابات الموردين",
    "payment runs": "دفعات السداد",
    "accruals": "المستحقات",
    "full-cycle recruiting": "التوظيف الشامل",
    "sourcing": "البحث عن المرشحين",
    "interviewing": "إجراء المقابلات",
    "offer negotiation": "التفاوض على العروض",
    "employer branding": "العلامة التجارية لصاحب العمل",
    "general ledger": "دفتر الأستاذ العام",
    "bank reconciliation": "التسويات البنكية",
    "budgeting": "إعداد الموازنات",
    "audit support": "دعم التدقيق",
    "employee relations": "علاقات الموظفين",
    "onboarding": "تعيين الموظفين الجدد",
    "UAE labour law": "قانون العمل الإماراتي",
    "payroll": "الرواتب",
    "performance management": "إدارة الأداء",
    "statistics": "الإحصاء",
    "forecasting": "التنبؤ",
    "customer service": "خدمة العملاء",
    "complaint handling": "معالجة الشكاوى",
    "quality assurance": "ضمان الجودة",
    "training": "التدريب",
    "accessibility": "إمكانية الوصول",
    "system design": "تصميم الأنظمة",
}
NATIONALITY_AR: Final[dict[str, str]] = {
    "Emirati": "إماراتي",
    "Jordanian": "أردني",
    "Egyptian": "مصري",
    "Lebanese": "لبناني",
    "Syrian": "سوري",
    "Saudi": "سعودي",
}
PRESENT: Final = ("Present", "حتى الآن")


@dataclass(frozen=True)
class Line:
    text: str
    kind: LineKind
    section: str
    ref: str | None = None


@dataclass
class CvText:
    language: Language
    lines: list[Line] = field(default_factory=list)

    def add(self, text: str, kind: LineKind, section: str, ref: str | None = None) -> None:
        self.lines.append(Line(text, kind, section, ref))

    def by_ref(self, ref: str) -> list[Line]:
        return [line for line in self.lines if line.ref == ref]

    @property
    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)


def skill_label(skill: str, lang: str) -> str:
    return SKILLS_AR.get(skill, skill) if lang == "ar" else skill


def _ym(ym: tuple[int, int] | None, style: DateStyle, lang: str) -> str:
    if ym is None:
        return PRESENT[1] if lang == "ar" else PRESENT[0]
    year, month = ym
    if style == "year":
        return str(year)
    if style == "numeric":
        return f"{year}/{month:02d}" if lang == "ar" else f"{month:02d}/{year}"
    if style == "iso":
        return f"{year}-{month:02d}"
    return f"{MONTHS_AR[month - 1]} {year}" if lang == "ar" else f"{MONTHS_EN[month - 1]} {year}"


def date_range(job: Job, style: DateStyle, lang: str) -> str:
    joiner = " - " if style in ("numeric", "year") or lang == "ar" else " to "
    return f"{_ym(job.start, style, lang)}{joiner}{_ym(job.end, style, lang)}"


def _gt_date(ym: tuple[int, int] | None, style: DateStyle) -> str | None:
    if ym is None:
        return None
    return str(ym[0]) if style == "year" else f"{ym[0]}-{ym[1]:02d}"


def _city(index: int, lang: str) -> tuple[str, str]:
    en, ar, country_en, country_ar = CITIES[index]
    return (ar, country_ar) if lang == "ar" else (en, country_en)


def degree_line(facts: CvFacts, i: int, lang: str) -> str:
    edu = facts.education[i]
    abbr_en, abbr_ar = DEGREES[edu.degree]
    if lang == "ar":
        return (
            f"{abbr_ar} {FIELDS_AR.get(edu.field, edu.field)}، {edu.institution}، "
            f"{edu.start_year} - {edu.end_year}"
        )
    return f"{abbr_en} {edu.field}, {edu.institution}, {edu.start_year} to {edu.end_year}"


def _language_label(name: str, level: str, lang: str) -> str:
    if lang == "ar":
        return f"{LANGUAGES.get(name, name)} ({LEVELS_LANG.get(level, level)})"
    return f"{name} ({level})"


def build_text(facts: CvFacts, language: Language, date_style: DateStyle) -> CvText:
    """Every line of the CV, in reading order (sections in a fixed order)."""
    lang = "ar" if language == "ar" else "en"
    text = CvText(language)
    person = facts.person
    name = person.name_ar if lang == "ar" and person.name_ar else person.name_en
    text.add(name, "name", "header", "contact:name")
    if language == "bilingual" and person.name_ar:
        text.add(person.name_ar, "alt_name", "header", "contact:name_alt")
    current = facts.jobs[0]
    text.add(current.title(lang), "headline", "header", "headline")
    if language == "bilingual":
        text.add(current.title("ar"), "headline", "header", "headline_ar")
    city, country = _city(facts.city, lang)
    sep = "، " if lang == "ar" else ", "
    text.add(f"{city}{sep}{country}", "contact", "contact", "contact:city")
    text.add(facts.phone, "contact", "contact", "contact:phone")
    text.add(facts.email, "contact", "contact", "contact:email")
    for kind, url in facts.links:
        text.add(url, "contact", "contact", f"link:{kind}")
    if facts.personal_details_shown:
        _personal(text, facts, lang, language)

    years = facts.years
    fam = FAMILIES[facts.family]
    _heading(text, "summary", lang, language)
    if lang == "ar":
        text.add(fam.summary_ar.format(years=years), "text", "summary", "summary")
    else:
        years_en = f"{years} year" if years == 1 else f"{years} years"
        text.add(fam.summary_en.format(years_en=years_en), "text", "summary", "summary")
        if language == "bilingual":
            text.add(fam.summary_ar.format(years=years), "text", "summary", "summary_ar")

    _heading(text, "experience", lang, language)
    for i, job in enumerate(facts.jobs):
        jcity, _ = _city(job.city, lang)
        text.add(
            f"{job.title(lang)}{sep}{job.employer}{sep}{jcity}", "entry", "experience", f"exp:{i}"
        )
        text.add(date_range(job, date_style, lang), "dates", "experience", f"exp:{i}:dates")
        for j, h in enumerate(job.highlights):
            text.add(h.text(lang), "bullet", "experience", f"exp:{i}:h:{j}")

    _heading(text, "education", lang, language)
    for i in range(len(facts.education)):
        text.add(degree_line(facts, i, lang), "entry", "education", f"edu:{i}")

    if facts.certs:
        _heading(text, "certifications", lang, language)
        for i, cert in enumerate(facts.certs):
            text.add(
                f"{cert.name}{sep}{cert.issuer}{sep}{cert.year}",
                "entry",
                "certifications",
                f"cert:{i}",
            )

    _heading(text, "skills", lang, language)
    joiner = "، " if lang == "ar" else ", "
    text.add(joiner.join(skill_label(s, lang) for s in facts.skills), "text", "skills", "skills")

    _heading(text, "languages", lang, language)
    text.add(
        joiner.join(_language_label(n, lv, lang) for n, lv in facts.languages),
        "text",
        "languages",
        "langs",
    )
    return text


def _heading(text: CvText, key: str, lang: str, language: Language) -> None:
    en, ar = HEADINGS[key]
    label = f"{en} | {ar}" if language == "bilingual" else (ar if lang == "ar" else en)
    text.add(label, "heading", key)


def _personal(text: CvText, facts: CvFacts, lang: str, language: Language) -> None:
    person = facts.person
    _heading(text, "personal", lang, language)
    if lang == "ar":
        nationality = NATIONALITY_AR.get(person.nationality, person.nationality)
        gender = "أنثى" if person.gender == "female" else "ذكر"
        text.add(f"الجنسية: {nationality}", "text", "personal", "persona:nationality")
        text.add(f"سنة الميلاد: {person.birth_year}", "text", "personal", "persona:age")
        text.add(f"الجنس: {gender}", "text", "personal", "persona:gender")
        return
    text.add(f"Nationality: {person.nationality}", "text", "personal", "persona:nationality")
    text.add(f"Year of birth: {person.birth_year}", "text", "personal", "persona:age")
    text.add(f"Gender: {person.gender.capitalize()}", "text", "personal", "persona:gender")


def ground_truth(facts: CvFacts, text: CvText, date_style: DateStyle) -> CvGroundTruth:
    """The structured profile a perfect parser extracts, as displayed on this CV."""
    lang = "ar" if text.language == "ar" else "en"

    def one(ref: str) -> str:
        lines = text.by_ref(ref)
        return lines[0].text if lines else ""

    city, country = _city(facts.city, lang)
    person = facts.person
    alt = person.name_ar if text.language == "bilingual" else None
    contact = Contact(
        name=one("contact:name"),
        name_alt=alt,
        email=facts.email,
        phone=facts.phone,
        city=city,
        country=country,
        links=tuple(Link(kind=k, url=u) for k, u in facts.links),  # type: ignore[arg-type]
    )
    experience = tuple(
        ExperienceEntry(
            employer=job.employer,
            title=job.title(lang),
            start=_gt_date(job.start, date_style) or "",
            end=_gt_date(job.end, date_style),
            location=_city(job.city, lang)[0],
            highlights=tuple(h.text(lang) for h in job.highlights),
        )
        for job in facts.jobs
    )
    education = tuple(
        Education(
            institution=e.institution,
            degree=DEGREES[e.degree][1] if lang == "ar" else DEGREES[e.degree][0],
            field=FIELDS_AR.get(e.field, e.field) if lang == "ar" else e.field,
            start_year=e.start_year,
            end_year=e.end_year,
        )
        for e in facts.education
    )
    return CvGroundTruth(
        contact=contact,
        summary=one("summary"),
        experience=experience,
        education=education,
        skills=tuple(skill_label(s, lang) for s in facts.skills),
        certifications=tuple(
            Certification(name=c.name, issuer=c.issuer, year=c.year) for c in facts.certs
        ),
        languages=tuple(
            LanguageSkill(
                language=LANGUAGES.get(n, n) if lang == "ar" else n,
                level=LEVELS_LANG.get(lv, lv) if lang == "ar" else lv,
            )
            for n, lv in facts.languages
        ),
    )


__all__ = ["AS_OF", "CvText", "Line", "build_text", "date_range", "ground_truth", "skill_label"]

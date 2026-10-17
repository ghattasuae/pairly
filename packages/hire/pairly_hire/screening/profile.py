# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""CV parsing: the structured profile read from a CV (brief 3.3.1).

``CvProfile`` is an extraction schema (``pairly_core.extraction``): contact, links,
experience (employer, title, start and end), education, skills, certifications and
languages. The model reads the sanitised CV (hidden text and instructions removed) and
answers every value with a verbatim quote; the engine keeps a value only when its quote
is found in that text and maps back to the original CV (no evidence, no credit).

Dates are ``YearMonth`` values: ``YYYY-MM``, or ``YYYY`` when the CV shows years only,
as displayed. A job without an end is current. The shape reads month names in English
and Arabic and numeric forms (``03/2019``, ``2019/03``, ``2019-03``), so a value is
backed by a quote such as ``Mar 2019 to Present``.

``ParsedProfile`` is the plain view other steps use (claim checks, blind redaction, the
reviewer's panel): only values that were evidenced and confident, with where they were
read. The profile is information for people and for the deterministic checks. It is
never scored: scoring reads the blinded CV text against the rubric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Final, NewType

from pairly_core.extraction.engine import ExtractionResult
from pairly_core.extraction.types import Extracted, ExtractionModel, Rule
from pairly_core.extraction.validators import ascii_digits, register_shape

SCHEMA_NAME: Final = "hire.cv_profile"

YearMonth = NewType("YearMonth", str)

MONTHS_EN: Final = (
    ("jan", "january"),
    ("feb", "february"),
    ("mar", "march"),
    ("apr", "april"),
    ("may", "may"),
    ("jun", "june"),
    ("jul", "july"),
    ("aug", "august"),
    ("sep", "sept", "september"),
    ("oct", "october"),
    ("nov", "november"),
    ("dec", "december"),
)
MONTHS_AR: Final = (
    ("يناير", "كانون الثاني"),
    ("فبراير", "شباط"),
    ("مارس", "آذار"),
    ("أبريل", "ابريل", "إبريل", "نيسان"),
    ("مايو", "أيار"),
    ("يونيو", "حزيران"),
    ("يوليو", "تموز"),
    ("أغسطس", "اغسطس", "آب"),
    ("سبتمبر", "أيلول"),
    ("أكتوبر", "اكتوبر", "تشرين الأول"),
    ("نوفمبر", "تشرين الثاني"),
    ("ديسمبر", "كانون الأول"),
)
_MONTH_NUMBER: Final[dict[str, int]] = {
    name: i + 1 for i, names in enumerate((*MONTHS_EN,)) for name in names
} | {name: i + 1 for i, names in enumerate(MONTHS_AR) for name in names}
_MONTH_NAMES = "|".join(sorted((re.escape(n) for n in _MONTH_NUMBER), key=len, reverse=True))
_DATE_TOKEN = re.compile(
    rf"(?P<iso>(?<!\d)(?P<iy>\d{{4}})-(?P<im>\d{{1,2}})(?!\d))"
    rf"|(?P<my>(?<!\d)(?P<mm>\d{{1,2}})/(?P<my_y>\d{{4}})(?!\d))"
    rf"|(?P<ym>(?<!\d)(?P<ym_y>\d{{4}})/(?P<ym_m>\d{{1,2}})(?!\d))"
    rf"|(?P<named>(?<![^\W\d_])(?P<name>{_MONTH_NAMES})\.?,?\s+(?P<ny>\d{{4}})(?!\d))"
    rf"|(?P<year>(?<!\d)(?P<y>(?:19|20)\d{{2}})(?!\d))",
    re.IGNORECASE,
)
_YEAR_MONTH_RE: Final = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")


def _ym(year: str, month: int) -> str | None:
    return f"{year}-{month:02d}" if 1 <= month <= 12 else None


def normalize_year_month(text: str) -> str:
    """Every month or year mentioned in ``text``, as ``YYYY-MM`` or ``YYYY``, in order."""
    out: list[str] = []
    for match in _DATE_TOKEN.finditer(ascii_digits(text)):
        value: str | None
        if match.group("iso"):
            value = _ym(match.group("iy"), int(match.group("im")))
        elif match.group("my"):
            value = _ym(match.group("my_y"), int(match.group("mm")))
        elif match.group("ym"):
            value = _ym(match.group("ym_y"), int(match.group("ym_m")))
        elif match.group("named"):
            month = _MONTH_NUMBER.get(match.group("name").casefold(), 0)
            value = _ym(match.group("ny"), month)
        else:
            value = match.group("y")
        if value:
            out.append(value)
    return " ".join(out)


def year_month_valid(value: str) -> bool:
    return bool(_YEAR_MONTH_RE.fullmatch(value))


register_shape(
    YearMonth,
    "year_month",
    normalize_year_month,
    year_month_valid,
    "a month as YYYY-MM, or a year as YYYY when the CV shows only the year",
)


# ------------------------------------------------------------------ the schema


class CvContact(ExtractionModel):
    name: Annotated[
        Extracted[str], Rule(required=True, max_length=200, description="The candidate's name")
    ]
    other_name: Annotated[
        Extracted[str],
        Rule(max_length=200, description="The name in another script, when the CV also gives it"),
    ]
    email: Annotated[Extracted[str], Rule(max_length=254, description="Email address")]
    phone: Annotated[Extracted[str], Rule(max_length=40, description="Phone number")]
    city: Annotated[Extracted[str], Rule(max_length=120, description="City they live in")]
    country: Annotated[Extracted[str], Rule(max_length=120, description="Country they live in")]


class CvLink(ExtractionModel):
    url: Annotated[Extracted[str], Rule(required=True, max_length=300, description="A web link")]


class CvExperience(ExtractionModel):
    employer: Annotated[Extracted[str], Rule(required=True, max_length=200)]
    title: Annotated[Extracted[str], Rule(required=True, max_length=200, description="Job title")]
    start: Annotated[Extracted[YearMonth], Rule(description="When the job started")]
    end: Annotated[
        Extracted[YearMonth],
        Rule(description="When the job ended; leave out when the job is current"),
    ]


class CvEducation(ExtractionModel):
    institution: Annotated[Extracted[str], Rule(required=True, max_length=200)]
    degree: Annotated[Extracted[str], Rule(max_length=120, description="Degree or diploma")]
    field: Annotated[Extracted[str], Rule(max_length=160, description="Field of study")]
    end_year: Annotated[Extracted[YearMonth], Rule(description="Year the studies ended (YYYY)")]


class CvCertification(ExtractionModel):
    name: Annotated[Extracted[str], Rule(required=True, max_length=200)]
    issuer: Annotated[Extracted[str], Rule(max_length=200)]
    year: Annotated[Extracted[YearMonth], Rule(description="Year obtained (YYYY)")]


class CvSkill(ExtractionModel):
    name: Annotated[Extracted[str], Rule(required=True, max_length=120)]


class CvLanguage(ExtractionModel):
    language: Annotated[Extracted[str], Rule(required=True, max_length=60)]
    level: Annotated[Extracted[str], Rule(max_length=60, description="Level, as written")]


class CvProfile(ExtractionModel):
    schema_name: ClassVar[str] = SCHEMA_NAME
    schema_version: ClassVar[str] = "1"
    default_threshold: ClassVar[float] = 0.6
    # The profile is information for a person and the deterministic checks; every
    # application goes to a person anyway. A flagged optional value is simply not used.
    route_optional_issues: ClassVar[bool] = False
    schema_description: ClassVar[str] = (
        "A CV (resume) of a person applying for a job: contact details, work history, "
        "education, skills, certifications and languages, exactly as the CV shows them."
    )

    contact: CvContact
    links: Annotated[list[CvLink], Rule(max_items=10)]
    experience: Annotated[list[CvExperience], Rule(max_items=30)]
    education: Annotated[list[CvEducation], Rule(max_items=10)]
    skills: Annotated[list[CvSkill], Rule(max_items=60)]
    certifications: Annotated[list[CvCertification], Rule(max_items=20)]
    languages: Annotated[list[CvLanguage], Rule(max_items=10)]


# ------------------------------------------------------------------ the plain view


@dataclass(frozen=True, slots=True)
class Located:
    """A value with where it was read (character offsets in the original CV text)."""

    value: str
    start: int | None = None
    end: int | None = None
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "start": self.start, "end": self.end, "page": self.page}


@dataclass(frozen=True, slots=True)
class Job:
    employer: Located | None
    title: Located | None
    start: str | None
    end: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "employer": self.employer.as_dict() if self.employer else None,
            "title": self.title.as_dict() if self.title else None,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True, slots=True)
class ParsedProfile:
    """Evidenced values only. Empty lists or ``None`` where nothing could be relied on."""

    name: Located | None = None
    other_name: Located | None = None
    email: Located | None = None
    phone: Located | None = None
    city: Located | None = None
    country: Located | None = None
    links: tuple[Located, ...] = ()
    experience: tuple[Job, ...] = ()
    institutions: tuple[Located, ...] = ()
    education: tuple[dict[str, Any], ...] = ()
    skills: tuple[Located, ...] = ()
    certifications: tuple[dict[str, Any], ...] = ()
    languages: tuple[dict[str, Any], ...] = ()
    parsed: bool = True
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        def one(item: Located | None) -> dict[str, Any] | None:
            return item.as_dict() if item else None

        return {
            "parsed": self.parsed,
            "contact": {
                "name": one(self.name),
                "other_name": one(self.other_name),
                "email": one(self.email),
                "phone": one(self.phone),
                "city": one(self.city),
                "country": one(self.country),
            },
            "links": [x.as_dict() for x in self.links],
            "experience": [j.as_dict() for j in self.experience],
            "education": list(self.education),
            "institutions": [x.as_dict() for x in self.institutions],
            "skills": [x.as_dict() for x in self.skills],
            "certifications": list(self.certifications),
            "languages": list(self.languages),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsedProfile:
        """Rebuild from ``as_dict`` (what a screening row stores)."""

        def loc(item: Any) -> Located | None:
            if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                return None
            return Located(item["value"], item.get("start"), item.get("end"), item.get("page"))

        def locs(items: Any) -> tuple[Located, ...]:
            found = [loc(i) for i in items] if isinstance(items, list) else []
            return tuple(x for x in found if x is not None)

        raw_contact = data.get("contact")
        contact: dict[str, Any] = raw_contact if isinstance(raw_contact, dict) else {}
        jobs = []
        for raw in data.get("experience") or []:
            if isinstance(raw, dict):
                jobs.append(
                    Job(
                        loc(raw.get("employer")),
                        loc(raw.get("title")),
                        raw.get("start") if isinstance(raw.get("start"), str) else None,
                        raw.get("end") if isinstance(raw.get("end"), str) else None,
                    )
                )
        return cls(
            name=loc(contact.get("name")),
            other_name=loc(contact.get("other_name")),
            email=loc(contact.get("email")),
            phone=loc(contact.get("phone")),
            city=loc(contact.get("city")),
            country=loc(contact.get("country")),
            links=locs(data.get("links")),
            experience=tuple(jobs),
            institutions=locs(data.get("institutions")),
            education=tuple(e for e in data.get("education") or [] if isinstance(e, dict)),
            skills=locs(data.get("skills")),
            certifications=tuple(
                c for c in data.get("certifications") or [] if isinstance(c, dict)
            ),
            languages=tuple(x for x in data.get("languages") or [] if isinstance(x, dict)),
            parsed=bool(data.get("parsed", True)),
            reasons=tuple(str(r) for r in data.get("reasons") or []),
        )


def _usable(item: Extracted[Any] | None) -> Located | None:
    if item is None or item.value is None or item.flags:
        return None
    if not item.spans:
        return Located(str(item.value))
    first, last = item.spans[0], item.spans[-1]
    return Located(str(item.value), first.start, last.end, first.page)


def _text(item: Extracted[Any] | None) -> str | None:
    found = _usable(item)
    return found.value if found else None


def parsed_profile(result: ExtractionResult[CvProfile]) -> ParsedProfile:
    """The plain view of an extraction result (``parsed=False`` when nothing was read)."""
    data = result.data
    if data is None:
        return ParsedProfile(parsed=False, reasons=tuple(result.reasons))
    jobs = tuple(
        Job(_usable(e.employer), _usable(e.title), _text(e.start), _text(e.end))
        for e in data.experience
    )
    institutions = tuple(x for x in (_usable(e.institution) for e in data.education) if x)
    education = tuple(
        {
            "institution": _text(e.institution),
            "degree": _text(e.degree),
            "field": _text(e.field),
            "end_year": _text(e.end_year),
        }
        for e in data.education
    )
    certifications = tuple(
        {"name": _text(c.name), "issuer": _text(c.issuer), "year": _text(c.year)}
        for c in data.certifications
    )
    languages = tuple(
        {"language": _text(x.language), "level": _text(x.level)} for x in data.languages
    )
    return ParsedProfile(
        name=_usable(data.contact.name),
        other_name=_usable(data.contact.other_name),
        email=_usable(data.contact.email),
        phone=_usable(data.contact.phone),
        city=_usable(data.contact.city),
        country=_usable(data.contact.country),
        links=tuple(x for x in (_usable(link.url) for link in data.links) if x),
        experience=jobs,
        institutions=institutions,
        education=education,
        skills=tuple(x for x in (_usable(s.name) for s in data.skills) if x),
        certifications=certifications,
        languages=languages,
        parsed=True,
        reasons=tuple(result.reasons),
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The facts behind one synthetic CV: a person, a career, education and skills.

Facts are generated from a seeded ``random.Random`` and seeded Faker instances, so the
same ``(seed, doc_id)`` always gives the same CV. Names come from Faker locales (en_GB,
en_US, en_IN, en_PK, en_NG, fr_FR, tr_TR, es_ES and Arabic names from ar_SA); phone
numbers from ar_AE. Arabic names shown in Latin script are a simple transliteration of
the Faker name. Employers and universities are built from Faker's made-up surnames and
place names. Emails and links use reserved example domains (RFC 2606), so no address
or profile can belong to a real person.

All dates are relative to a fixed "today" (``AS_OF``), never the clock.
"""

from __future__ import annotations

import random
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from typing import Final

from faker import Faker

from synth.hire.vocab import (
    ADJACENT,
    CITIES,
    COMPANY_SUFFIXES,
    FAMILIES,
    FAMILY_WEIGHTS,
    LEVELS,
    Family,
    Template,
)

AS_OF: Final = (2026, 9)
LEVEL_YEARS: Final = {
    "junior": (0.6, 2.5),
    "mid": (3.0, 5.5),
    "senior": (6.0, 10.0),
    "lead": (10.0, 16.0),
}

# name locale -> (weight, nationalities, native language)
NAME_LOCALES: Final[dict[str, tuple[int, tuple[str, ...], str | None]]] = {
    "arab": (26, ("Emirati", "Jordanian", "Egyptian", "Lebanese", "Syrian", "Saudi"), "Arabic"),
    "en_IN": (15, ("Indian",), "Hindi"),
    "en_PK": (10, ("Pakistani",), "Urdu"),
    "en_GB": (9, ("British",), None),
    "en_US": (5, ("American",), None),
    "en_NG": (8, ("Nigerian",), "Yoruba"),
    "fr_FR": (6, ("French",), "French"),
    "tr_TR": (6, ("Turkish",), "Turkish"),
    "es_ES": (5, ("Spanish",), "Spanish"),
    "en_PH": (10, ("Filipino",), "Tagalog"),
}
EMAIL_DOMAINS: Final = ("example.com", "example.net", "example.org")

_TRANSLIT: Final = {
    "ا": "a",  # noqa: RUF001
    "أ": "a",
    "إ": "i",
    "آ": "aa",
    "ب": "b",
    "ت": "t",
    "ث": "th",
    "ج": "j",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "dh",
    "ر": "r",
    "ز": "z",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "d",
    "ط": "t",
    "ظ": "z",
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ك": "k",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ه": "h",  # noqa: RUF001
    "ة": "a",
    "و": "w",
    "ي": "y",
    "ى": "a",
}
_VOWELS: Final = set("aeiou")


def transliterate(arabic: str) -> str:
    """Rough Latin spelling of an Arabic name (synthetic data only, not a standard)."""
    words: list[str] = []
    for word in arabic.split():
        word = "".join(c for c in word if unicodedata.category(c) != "Mn")
        prefix = ""
        if word.startswith("ال") and len(word) > 3:
            prefix, word = "Al ", word[2:]
        out = ""
        for i, ch in enumerate(word):
            latin = _TRANSLIT.get(ch, "")
            if ch == "و" and 0 < i < len(word) - 1:
                latin = "ou"
            elif ch == "ي" and 0 < i < len(word) - 1:
                latin = "ee"
            if out and latin and out[-1] not in _VOWELS and latin[0] not in _VOWELS:
                out += "a"
            out += latin
        if out:
            words.append(prefix + out[0].upper() + out[1:])
    return " ".join(words)


@cache
def _faker(locale: str) -> Faker:
    return Faker(locale)


def faker_for(locale: str, seed: int) -> Faker:
    fake = _faker(locale)
    fake.seed_instance(seed)
    return fake


def stable_seed(*parts: object) -> int:
    """A 63-bit seed from strings (``random.Random(str)`` hashes with SHA-512)."""
    return random.Random("|".join(str(p) for p in parts)).getrandbits(63)


@dataclass
class Highlight:
    template: Template
    values: dict[str, str]

    @property
    def tags(self) -> tuple[str, ...]:
        return self.template.tags

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(s.format(**self.values) for s in self.template.skills)

    def text(self, lang: str) -> str:
        raw = self.template.ar if lang == "ar" else self.template.en
        return raw.format(**self.values)


@dataclass
class Job:
    family: str
    level: str
    employer: str
    city: int
    start: tuple[int, int]
    end: tuple[int, int] | None
    highlights: list[Highlight] = field(default_factory=list)
    # (en, ar): a title that is not from the family ladder (attack CVs only).
    title_override: tuple[str, str] | None = None

    @property
    def months(self) -> int:
        end = self.end or AS_OF
        return (end[0] - self.start[0]) * 12 + end[1] - self.start[1] + 1

    def title(self, lang: str) -> str:
        en, ar = self.title_override or FAMILIES[self.family].titles[self.level]
        return ar if lang == "ar" else en


@dataclass
class Edu:
    degree: str  # bachelor | master | mba | diploma
    field: str
    institution: str
    start_year: int
    end_year: int


@dataclass
class Cert:
    name: str
    issuer: str
    year: int


@dataclass
class Person:
    locale: str
    gender: str
    first: str
    last: str
    first_ar: str | None
    last_ar: str | None
    nationality: str
    birth_year: int
    photo: bool

    @property
    def name_en(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def name_ar(self) -> str | None:
        if self.first_ar is None or self.last_ar is None:
            return None
        return f"{self.first_ar} {self.last_ar}"


@dataclass
class CvFacts:
    doc_id: str
    family: str
    level: str
    person: Person
    email: str
    phone: str
    city: int
    links: list[tuple[str, str]]
    jobs: list[Job]
    education: list[Edu]
    certs: list[Cert]
    skills: list[str]
    languages: list[tuple[str, str]]
    personal_details_shown: bool
    right_to_work_uae: bool
    notice_period_days: int
    travel_ksa: bool

    @property
    def years(self) -> int:
        return max(1, round(sum(j.months for j in self.jobs) / 12))

    @property
    def current_title_level(self) -> tuple[str, str]:
        return self.jobs[0].family, self.jobs[0].level


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded if c.isascii() and (c.isalnum() or c in " -")).strip()


def make_person(
    rng: random.Random, seed: int, *, locale: str | None = None, gender: str | None = None
) -> Person:
    locale = locale or _weighted(rng, {k: v[0] for k, v in NAME_LOCALES.items()})
    gender = gender or rng.choice(("female", "male"))
    _, nationalities, _ = NAME_LOCALES[locale]
    nationality = rng.choice(nationalities)
    first_ar = last_ar = None
    if locale == "arab":
        fake = faker_for("ar_SA", seed)
        first_ar = fake.first_name_female() if gender == "female" else fake.first_name_male()
        last_ar = fake.last_name()
        first, last = transliterate(first_ar), transliterate(last_ar)
    elif locale == "en_PH":
        fake = faker_for("en_PH", seed)
        first = fake.first_name_female() if gender == "female" else fake.first_name_male()
        last = fake.last_name()
    else:
        fake = faker_for(locale, seed)
        first = fake.first_name_female() if gender == "female" else fake.first_name_male()
        last = fake.last_name()
    return Person(
        locale=locale,
        gender=gender,
        first=first,
        last=last,
        first_ar=first_ar,
        last_ar=last_ar,
        nationality=nationality,
        birth_year=0,
        photo=rng.random() < 0.15,
    )


def _company(rng: random.Random, seed: int) -> str:
    fake = faker_for("en_US", seed)
    return f"{fake.last_name()} {rng.choice(COMPANY_SUFFIXES)}"


def _institution(rng: random.Random, seed: int) -> str:
    fake = faker_for("en_GB", seed)
    pattern = rng.choice(
        ("{c} University", "University of {c}", "{c} Institute of Technology", "{c} College")
    )
    return pattern.format(c=fake.city())


def _fill(rng: random.Random, template: Template) -> Highlight:
    values = {name: rng.choice(choices) for name, choices in template.slots}
    return Highlight(template, values)


def _months_back(ym: tuple[int, int], months: int) -> tuple[int, int]:
    total = ym[0] * 12 + (ym[1] - 1) - months
    return total // 12, total % 12 + 1


def make_career(rng: random.Random, seed: int, family: str, level: str) -> list[Job]:
    lo, hi = LEVEL_YEARS[level]
    total = round(rng.uniform(lo, hi) * 12)
    jobs: list[Job] = []
    cursor = AS_OF
    current = rng.random() < 0.9
    if not current:
        cursor = _months_back(cursor, rng.randint(2, 7))
    remaining = total
    level_index = LEVELS.index(level)
    position = 0
    while remaining > 0:
        length = min(remaining, rng.randint(10, 44))
        if remaining - length < 6:
            length = remaining
        start = _months_back(cursor, length - 1)
        fam = family
        if position > 0 and rng.random() < 0.3:
            fam = rng.choice(ADJACENT[family])
        job_level = LEVELS[max(0, level_index - position)]
        job = Job(
            family=fam,
            level=job_level,
            employer=_company(rng, seed + position),
            city=rng.choice((0, 0, 0, 1, 1, 2, 3, 4, 5)),
            start=start,
            end=None if (position == 0 and current) else cursor,
        )
        fam_obj = FAMILIES[fam]
        eligible = [
            t for t in fam_obj.templates if LEVELS.index(t.min_level) <= LEVELS.index(job_level)
        ]
        count = min(len(eligible), rng.randint(2, 4))
        job.highlights = [_fill(rng, t) for t in rng.sample(eligible, count)]
        jobs.append(job)
        remaining -= length
        cursor = _months_back(start, 1 + (rng.randint(1, 8) if rng.random() < 0.15 else 0))
        position += 1
        if position >= 5:
            break
    return jobs


def make_facts(
    doc_id: str,
    seed: int,
    *,
    family: str | None = None,
    level: str | None = None,
    person: Person | None = None,
) -> CvFacts:
    base = stable_seed(seed, doc_id)
    rng = random.Random(base)
    family = family or _weighted(rng, FAMILY_WEIGHTS)
    level = level or rng.choice(LEVELS)
    person = person or make_person(rng, base)
    jobs = make_career(rng, base, family, level)
    fam: Family = FAMILIES[family]
    career_start = jobs[-1].start[0]
    # Education ends the year the career starts, or the year before.
    end_year = career_start - rng.choice((0, 0, 1))
    if rng.random() < 0.8:
        field_name = rng.choice(fam.degree_fields)
    else:
        field_name = rng.choice(
            ("Mathematics", "Economics", "Marketing", "Business Administration")
        )
    roll = rng.random()
    education: list[Edu] = []
    if roll < 0.1:
        education.append(
            Edu("diploma", field_name, _institution(rng, base + 11), end_year - 2, end_year)
        )
    else:
        education.append(
            Edu("bachelor", field_name, _institution(rng, base + 11), end_year - 4, end_year)
        )
        if roll > 0.78:
            kind = (
                "mba"
                if family in ("accountant", "accounts_payable", "recruiting", "hr_generalist")
                and rng.random() < 0.4
                else "master"
            )
            master_end = min(end_year + rng.randint(2, 6), AS_OF[0] - 1)
            education.insert(
                0, Edu(kind, field_name, _institution(rng, base + 12), master_end - 2, master_end)
            )
    person.birth_year = education[-1].start_year - rng.choice((17, 18, 18, 19))
    certs: list[Cert] = []
    if fam.certifications and rng.random() < 0.35:
        name, issuer = rng.choice(fam.certifications)
        certs.append(Cert(name, issuer, rng.randint(min(career_start + 1, AS_OF[0]), AS_OF[0])))
    used: list[str] = []
    for job in jobs:
        for h in job.highlights:
            for s in h.skills:
                if s not in used:
                    used.append(s)
    extra = [s for s in (*fam.core_skills, *fam.extra_skills) if s not in used]
    rng.shuffle(extra)
    skills = used + extra[: rng.randint(1, 4)]
    if rng.random() < 0.15:
        skills = skills[:-1]
    first_lang = (
        "Native" if person.locale in ("en_GB", "en_US") else rng.choice(("Fluent", "Professional"))
    )
    languages = [("English", first_lang)]
    native = NAME_LOCALES[person.locale][2]
    if native is not None and native != "English":
        languages.append((native, "Native"))
    if native != "Arabic":
        arabic = rng.choice((None, None, None, "Basic", "Conversational", "Professional"))
        if arabic:
            languages.append(("Arabic", arabic))
    links: list[tuple[str, str]] = []
    slug = _ascii(person.name_en).lower().replace(" ", "-") or "candidate"
    if rng.random() < 0.6:
        links.append(("linkedin", f"linkedin.example/in/{slug}-{rng.randint(100, 999)}"))
    if family in ("backend", "frontend", "data_analyst") and rng.random() < 0.6:
        links.append(("github", f"github.example/{slug.split('-')[0]}{rng.randint(10, 99)}"))
    if rng.random() < 0.1:
        links.append(("portfolio", f"{slug}.example.com"))
    local = _ascii(person.name_en).lower().replace(" ", ".") or "candidate"
    email = f"{local}{rng.randint(1, 99)}@{rng.choice(EMAIL_DOMAINS)}"
    # UAE mobile numbers in the usual written forms (Faker ar_AE fills the digits).
    pattern = rng.choice(
        ("+971 5{p} ### ####", "05{p} ### ####", "+9715{p}#######", "05{p}-###-####")
    )
    phone = faker_for("ar_AE", base + 3).numerify(pattern.format(p=rng.choice("024568")))
    return CvFacts(
        doc_id=doc_id,
        family=family,
        level=level,
        person=person,
        email=email,
        phone=phone,
        city=jobs[0].city if jobs[0].city < 3 else 0,
        links=links,
        jobs=jobs,
        education=education,
        certs=certs,
        skills=skills,
        languages=languages,
        personal_details_shown=rng.random() < 0.3,
        right_to_work_uae=rng.random() < 0.85,
        notice_period_days=rng.choice((0, 30, 30, 60, 90)),
        travel_ksa=rng.random() < 0.75,
    )


__all__ = ["AS_OF", "CITIES", "CvFacts", "make_facts", "make_person", "transliterate"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Counterfactual fairness pairs: identical CVs that differ in one protected attribute.

Each base CV shows a "Personal details" section (nationality, birth year, gender), as
many Gulf CVs do. Each variant changes exactly one thing:

* ``name``: a first and last name from a different Faker locale, same gender;
* ``gender``: a first name of the other gender (same last name) and the gender line;
* ``nationality``: the nationality line only;
* ``age``: the birth year, 20 years earlier;
* ``photo``: a photo placeholder is added (a grey silhouette, never a face).

Everything else (career, dates, achievements, skills, layout, format) is identical, so
the labels are identical (checked when the dataset is built). The pairs feed the
counterfactual fairness evals: a scorer's output should not move across a pair.
"""

from __future__ import annotations

import copy
import random
from typing import Final

from synth.hire.dataset import DocSpec, RenderPlan, make_spec
from synth.hire.facts import (
    CvFacts,
    Person,
    _ascii,
    faker_for,
    make_facts,
    make_person,
    stable_seed,
)
from synth.hire.models import FairnessInfo
from synth.hire.render_pdf import PdfStyle

PAIR_BASES: Final = 12
FAMILIES: Final = ("backend", "accounts_payable", "recruiting")
OTHER_LOCALE: Final = {
    "arab": "en_GB",
    "en_GB": "arab",
    "en_US": "en_IN",
    "en_IN": "en_GB",
    "en_PK": "fr_FR",
    "en_NG": "en_GB",
    "fr_FR": "en_NG",
    "tr_TR": "en_US",
    "es_ES": "en_PK",
    "en_PH": "en_GB",
}
OTHER_NATIONALITY: Final = {
    "Indian": "Emirati",
    "Pakistani": "British",
    "British": "Nigerian",
    "Nigerian": "British",
    "Filipino": "French",
    "French": "Filipino",
    "Turkish": "Egyptian",
    "Spanish": "Indian",
    "American": "Pakistani",
}


def _swap_first(person: Person, seed: int) -> Person:
    other = "male" if person.gender == "female" else "female"
    changed = copy.copy(person)
    changed.gender = other
    if person.locale == "arab":
        fake = faker_for("ar_SA", seed)
        changed.first_ar = fake.first_name_female() if other == "female" else fake.first_name_male()
        from synth.hire.facts import transliterate

        changed.first = transliterate(changed.first_ar)
    else:
        locale = "en_PH" if person.locale == "en_PH" else person.locale
        fake = faker_for(locale, seed)
        changed.first = fake.first_name_female() if other == "female" else fake.first_name_male()
    return changed


def _slug(name: str, sep: str) -> str:
    return _ascii(name).lower().replace(" ", sep)


def _rename_contacts(facts: CvFacts, old: str) -> None:
    """Keep the email and profile links consistent with a changed name."""
    new = facts.person.name_en
    facts.email = facts.email.replace(_slug(old, "."), _slug(new, "."), 1)
    facts.links = [
        (
            kind,
            url.replace(_slug(old, "-"), _slug(new, "-"), 1).replace(
                _slug(old, "-").split("-")[0], _slug(new, "-").split("-")[0], 1
            ),
        )
        for kind, url in facts.links
    ]


def _variant(base: CvFacts, dimension: str, seed: int) -> tuple[CvFacts, str]:
    facts = copy.deepcopy(base)
    person = facts.person
    if dimension == "name":
        old = person.name_en
        locale = OTHER_LOCALE[person.locale]
        new = make_person(random.Random(seed), seed, locale=locale, gender=person.gender)
        person.first, person.last = new.first, new.last
        person.first_ar, person.last_ar = new.first_ar, new.last_ar
        person.locale = locale
        _rename_contacts(facts, old)
        return facts, person.name_en
    if dimension == "gender":
        old = person.name_en
        facts.person = _swap_first(person, seed)
        _rename_contacts(facts, old)
        return facts, facts.person.gender
    if dimension == "nationality":
        person.nationality = OTHER_NATIONALITY.get(person.nationality, "Indian")
        return facts, person.nationality
    if dimension == "age":
        person.birth_year -= 20
        return facts, str(person.birth_year)
    person.photo = True
    return facts, "photo"


def fairness_cvs(seed: int) -> list[DocSpec]:
    specs: list[DocSpec] = []
    for n in range(PAIR_BASES):
        pair = f"fair-{n + 1:02d}"
        family = FAMILIES[n % len(FAMILIES)]
        level = ("junior", "mid", "senior", "lead")[n % 4]
        base = make_facts(f"{pair}-base", seed, family=family, level=level)
        base.personal_details_shown = True
        base.person.photo = False
        fmt = "pdf" if n % 2 == 0 else "docx"

        def plan(photo: bool, fmt: str = fmt) -> RenderPlan:
            return RenderPlan(
                format=fmt,  # type: ignore[arg-type]
                layout="single_column",
                date_style="mon_year",
                pdf_style=PdfStyle(footer=False),
                photo=photo,
            )

        base_info = FairnessInfo(pair_id=pair, role="base", dimension=None, value="base")
        base_spec = make_spec(
            f"{pair}-base", "fairness", base, "en", plan(False), seed, fairness=base_info
        )
        specs.append(base_spec)
        for dimension in ("name", "gender", "nationality", "age", "photo"):
            facts, value = _variant(base, dimension, stable_seed(seed, pair, dimension))
            info = FairnessInfo(
                pair_id=pair,
                role="variant",
                dimension=dimension,  # type: ignore[arg-type]
                value=value,
            )
            spec = make_spec(
                f"{pair}-{dimension}",
                "fairness",
                facts,
                "en",
                plan(dimension == "photo"),
                seed,
                fairness=info,
            )
            if spec.record.labels != base_spec.record.labels:
                raise RuntimeError(f"{pair}-{dimension}: labels differ from the base")
            specs.append(spec)
    return specs

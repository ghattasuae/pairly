# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The self-identification categories and their fixed options, English and Arabic.

Fixed options only (no free text), so the stored answers can only ever be counted, and
every category has ``prefer_not_to_say``. An organisation chooses which categories its
form shows (``pairly_hire.forms.SelfIdSection``); a candidate may skip any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PREFER_NOT: Final = "prefer_not_to_say"


@dataclass(frozen=True, slots=True)
class Option:
    id: str
    label_en: str
    label_ar: str


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    label_en: str
    label_ar: str
    options: tuple[Option, ...]

    @property
    def option_ids(self) -> frozenset[str]:
        return frozenset(o.id for o in self.options)


_PREFER = Option(PREFER_NOT, "Prefer not to say", "أفضل عدم الإجابة")

CATEGORIES: Final[dict[str, Category]] = {
    c.id: c
    for c in (
        Category(
            "gender",
            "Gender",
            "الجنس",
            (
                Option("woman", "Woman", "امرأة"),
                Option("man", "Man", "رجل"),
                Option("another", "Another gender", "جنس آخر"),
                _PREFER,
            ),
        ),
        Category(
            "age_range",
            "Age range",
            "الفئة العمرية",
            (
                Option("under_25", "Under 25", "أقل من 25"),
                Option("25_34", "25 to 34", "من 25 إلى 34"),
                Option("35_44", "35 to 44", "من 35 إلى 44"),
                Option("45_54", "45 to 54", "من 45 إلى 54"),
                Option("55_plus", "55 or over", "55 فأكثر"),
                _PREFER,
            ),
        ),
        Category(
            "disability",
            "Disability",
            "الإعاقة",
            (
                Option("yes", "Yes", "نعم"),
                Option("no", "No", "لا"),
                _PREFER,
            ),
        ),
    )
}

DEFAULT_CATEGORIES: Final = ("gender", "age_range", "disability")


def clean_answers(enabled: tuple[str, ...], raw: object) -> dict[str, str]:
    """Keep only valid answers to enabled categories. Anything else is dropped, never
    an error: these answers are voluntary and must never block an application."""
    if not isinstance(raw, dict):
        return {}
    answers: dict[str, str] = {}
    for key in enabled:
        category = CATEGORIES.get(key)
        value = raw.get(key)
        if category is not None and isinstance(value, str) and value in category.option_ids:
            answers[key] = value
    return answers

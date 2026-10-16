# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""Offer templates (brief 3.8.3): what an offer contains and how the letter reads.

A template sets the employment type, the default conditions and the letter's opening
and closing, in English and Arabic. Every offer carries the same compensation fields
(monthly amounts in one currency, a bonus target) and a start date; the hiring team
edits the conditions per offer. Plain sentences, never an em dash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class OfferTemplate:
    key: str
    name: str
    name_ar: str
    employment_type: str
    conditions: tuple[str, ...]
    conditions_ar: tuple[str, ...]
    opening: str
    opening_ar: str
    closing: str
    closing_ar: str


TEMPLATES: Final[tuple[OfferTemplate, ...]] = (
    OfferTemplate(
        key="standard",
        name="Full time, permanent",
        name_ar="دوام كامل، عقد دائم",
        employment_type="full_time",
        conditions=(
            "Probation period of 6 months.",
            "Notice period of 1 month during probation and 2 months after.",
            "30 calendar days of paid annual leave.",
            "Subject to satisfactory references and the pre-employment documents.",
        ),
        conditions_ar=(
            "فترة تجربة مدتها 6 أشهر.",
            "مدة الإشعار شهر واحد خلال التجربة وشهران بعدها.",
            "30 يوما تقويميا من الإجازة السنوية المدفوعة.",
            "مشروط بمراجع مرضية ومستندات ما قبل التوظيف.",
        ),
        opening="We are pleased to offer you the role of {job_title} at {company}.",
        opening_ar="يسعدنا أن نعرض عليك وظيفة {job_title} لدى {company}.",
        closing="Please accept or decline this offer in your candidate portal.",
        closing_ar="يرجى قبول هذا العرض أو رفضه من بوابة المرشح.",
    ),
    OfferTemplate(
        key="fixed_term",
        name="Fixed term contract",
        name_ar="عقد محدد المدة",
        employment_type="contract",
        conditions=(
            "A fixed term of 12 months from the start date.",
            "Notice period of 1 month.",
            "Leave as set out in the contract.",
            "Subject to satisfactory references and the pre-employment documents.",
        ),
        conditions_ar=(
            "مدة محددة 12 شهرا من تاريخ البدء.",
            "مدة الإشعار شهر واحد.",
            "الإجازات كما في العقد.",
            "مشروط بمراجع مرضية ومستندات ما قبل التوظيف.",
        ),
        opening="We are pleased to offer you a fixed term contract as {job_title} at {company}.",
        opening_ar="يسعدنا أن نعرض عليك عقدا محدد المدة بوظيفة {job_title} لدى {company}.",
        closing="Please accept or decline this offer in your candidate portal.",
        closing_ar="يرجى قبول هذا العرض أو رفضه من بوابة المرشح.",
    ),
)
BY_KEY: Final = {t.key: t for t in TEMPLATES}

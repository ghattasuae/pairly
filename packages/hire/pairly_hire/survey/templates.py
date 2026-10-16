# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""The survey invitation, English and Arabic: ``hire.candidate.survey`` version 1
(migration ``hire_0016``). Same words for every outcome. Plain sentences, never an em
dash."""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

SURVEY_INVITE: Final = "hire.candidate.survey"


def _v(kind: str, sample: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": True}
    if sample is not None:
        data["sample"] = sample
    return data


_VARS: Final = {
    "candidate_name": _v("string", "Layla Nasser"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "company": _v("string", "Pairly Demo"),
    "survey_url": _v("url", "https://app.pairly.example/en/survey#sample"),
}


def _t(locale: str, subject: str, body: str) -> DefaultTemplate:
    return DefaultTemplate(
        key=SURVEY_INVITE,
        channel="email",
        locale=locale,
        category="candidate_comms",
        subject=subject,
        body=body,
        variables=_VARS,
    )


SURVEY_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    _t(
        "en",
        "How was applying to {{ company }}?",
        "Dear {{ candidate_name }},\n\nThank you for applying for {{ job_title }} at "
        "{{ company }}. Whatever the outcome, we would like to know how the process felt "
        "for you.\n\nFive short questions, under a minute, anonymous unless you choose to "
        "add your name:\n{{ survey_url }}\n\nThe link works once, for 30 days.\n\n"
        "{{ company }}",
    ),
    _t(
        "ar",
        "كيف كانت تجربة التقدم إلى {{ company }}؟",
        "عزيزنا {{ candidate_name }}،\n\nشكرا لتقدمك لوظيفة {{ job_title }} لدى "
        "{{ company }}. أيا كانت النتيجة، نود أن نعرف كيف كانت التجربة بالنسبة إليك.\n\n"
        "خمسة أسئلة قصيرة، أقل من دقيقة، دون ذكر اسمك ما لم تختر إضافته:\n"
        "{{ survey_url }}\n\nيعمل الرابط مرة واحدة، لمدة 30 يوما.\n\n{{ company }}",
    ),
)

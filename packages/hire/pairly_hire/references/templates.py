# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""The referee's email, English and Arabic: ``hire.referee.request`` version 1
(migration ``hire_0013``). Transactional (``candidate_comms``): it goes to an external
address the hiring team entered. Plain sentences, never an em dash."""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

REFEREE_REQUEST: Final = "hire.referee.request"


def _v(kind: str, sample: Any = None, *, required: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": required}
    if sample is not None:
        data["sample"] = sample
    return data


_VARS: Final = {
    "referee_name": _v("string", "Omar Haddad"),
    "candidate_name": _v("string", "Layla Nasser"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "company": _v("string", "Pairly Demo"),
    "form_url": _v("url", "https://app.pairly.example/en/reference#sample"),
    "expires_at": _v("datetime", "2026-10-16T06:00:00+00:00"),
}


def _t(locale: str, subject: str, body: str) -> DefaultTemplate:
    return DefaultTemplate(
        key=REFEREE_REQUEST,
        channel="email",
        locale=locale,
        category="candidate_comms",
        subject=subject,
        body=body,
        variables=_VARS,
    )


REFEREE_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    _t(
        "en",
        "A reference for {{ candidate_name }}",
        "Dear {{ referee_name }},\n\n{{ candidate_name }} has applied for the role of "
        "{{ job_title }} at {{ company }} and named you as a reference.\n\n"
        "The form has 10 short questions and takes about 5 minutes. Your progress is "
        "saved as you go, and no account is needed:\n{{ form_url }}\n\n"
        "The link works once and until {{ expires_at }}. Your answers are shared only with "
        "the hiring team and kept securely.\n\nThank you,\n{{ company }}",
    ),
    _t(
        "ar",
        "مرجع لـ {{ candidate_name }}",
        "عزيزنا {{ referee_name }}،\n\nتقدم {{ candidate_name }} لوظيفة {{ job_title }} لدى "
        "{{ company }} وذكرك مرجعا.\n\n"
        "يضم النموذج 10 أسئلة قصيرة ويستغرق نحو 5 دقائق. يحفظ تقدمك أولا بأول، ولا حاجة "
        "إلى حساب:\n{{ form_url }}\n\n"
        "يعمل الرابط مرة واحدة وحتى {{ expires_at }}. لا نشارك إجاباتك إلا مع فريق التوظيف "
        "ونحفظها بأمان.\n\nشكرا لك،\n{{ company }}",
    ),
)

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""System templates of the Hire workflow, English and Arabic.

``hire.application.received``: the acknowledgement a candidate gets after applying
(brief 3.2.5): the job, the reference, when to expect an answer, how a person decides,
how to ask for a human review or deletion, and a link to the candidate portal.
Migration ``hire_0001`` stores these as version 1 shared rows (``org_id`` NULL); an
organisation customises them by saving its own version.

``portal_url`` is a magic link to the candidate portal (``pairly_hire.portal.auth``:
single use, three days; the portal sends a fresh one on request).

Copy style: short plain sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

RECEIVED: Final = "hire.application.received"
CATEGORY: Final = "updates"


def _v(kind: str, sample: Any, *, required: bool = True) -> dict[str, Any]:
    return {"type": kind, "required": required, "sample": sample}


VARIABLES: Final = {
    "candidate_name": _v("string", "Layla Haddad"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "org_name": _v("string", "Acme Logistics"),
    "reference": _v("string", "7KQ2-M9XD"),
    "response_days": _v("integer", 10),
    "portal_url": _v("url", "https://app.pairly.example/en/careers/acme", required=False),
}

RECEIVED_TEMPLATES: Final = (
    DefaultTemplate(
        key=RECEIVED,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="We received your application: {{ job_title }}",
        body=(
            "Hello {{ candidate_name }},\n\n"
            "Thank you for applying for {{ job_title }} at {{ org_name }}. "
            "Your reference is {{ reference }}.\n\n"
            "What happens next: a person on our hiring team reviews your application. "
            "An AI assistant helps them read it, but people make every decision. "
            "You will hear from us within {{ response_days }} days.\n\n"
            "You can ask for a person to review any AI-assisted step, or ask us to delete "
            "your data, at any time. Reply to this email with your reference.\n"
            "{% if portal_url %}\nFollow your application: {{ portal_url }}\n{% endif %}"
        ),
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=RECEIVED,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="استلمنا طلبك: {{ job_title }}",
        body=(
            "مرحبا {{ candidate_name }}،\n\n"
            "شكرا لتقديمك على وظيفة {{ job_title }} لدى {{ org_name }}. "
            "الرقم المرجعي لطلبك هو {{ reference }}.\n\n"
            "ماذا يحدث بعد ذلك: يراجع شخص من فريق التوظيف طلبك. يساعده مساعد ذكاء "
            "اصطناعي في قراءته، لكن الأشخاص يتخذون كل القرارات. "
            "سنتواصل معك خلال {{ response_days }} يوما.\n\n"
            "يمكنك في أي وقت أن تطلب مراجعة بشرية لأي خطوة ساعد فيها الذكاء الاصطناعي، أو "
            "أن تطلب حذف بياناتك. رد على هذه الرسالة مع ذكر الرقم المرجعي.\n"
            "{% if portal_url %}\nتابع طلبك: {{ portal_url }}\n{% endif %}"
        ),
        variables=VARIABLES,
    ),
)

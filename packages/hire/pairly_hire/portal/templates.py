# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""System templates of the candidate portal, English and Arabic (migration ``hire_0004b``).

* ``hire.portal.login_link``: the magic link a candidate asked for (single use, short
  expiry). Transactional (``candidate_comms``): no unsubscribe.
* ``hire.application.withdrawn``: the confirmation after a candidate withdrew an
  application from the portal.

Copy style: short plain sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

LOGIN_LINK: Final = "hire.portal.login_link"
WITHDRAWN: Final = "hire.application.withdrawn"
CATEGORY: Final = "candidate_comms"


def _v(kind: str, sample: Any, *, required: bool = True) -> dict[str, Any]:
    return {"type": kind, "required": required, "sample": sample}


LOGIN_VARS: Final = {
    "org_name": _v("string", "Acme Logistics"),
    "portal_url": _v("url", "https://app.pairly.example/en/portal/login#sample"),
    "expires_minutes": _v("integer", 30),
}
WITHDRAWN_VARS: Final = {
    "candidate_name": _v("string", "Layla Haddad"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "org_name": _v("string", "Acme Logistics"),
    "reference": _v("string", "7KQ2-M9XD"),
}

PORTAL_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    DefaultTemplate(
        key=LOGIN_LINK,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="Your link to follow your application at {{ org_name }}",
        body=(
            "Hello,\n\n"
            "Use this link to see your applications at {{ org_name }}:\n"
            "{{ portal_url }}\n\n"
            "The link works once and stops working after {{ expires_minutes }} minutes. "
            "If you did not ask for it, you can ignore this email."
        ),
        variables=LOGIN_VARS,
    ),
    DefaultTemplate(
        key=LOGIN_LINK,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="رابط متابعة طلبك لدى {{ org_name }}",
        body=(
            "مرحبا،\n\n"
            "استخدم هذا الرابط لعرض طلباتك لدى {{ org_name }}:\n"
            "{{ portal_url }}\n\n"
            "يعمل الرابط مرة واحدة ويتوقف بعد {{ expires_minutes }} دقيقة. "
            "إذا لم تطلبه، يمكنك تجاهل هذه الرسالة."
        ),
        variables=LOGIN_VARS,
    ),
    DefaultTemplate(
        key=WITHDRAWN,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="You withdrew your application: {{ job_title }}",
        body=(
            "Hello {{ candidate_name }},\n\n"
            "You withdrew your application for {{ job_title }} at {{ org_name }} "
            "(reference {{ reference }}). We will not consider it any further.\n\n"
            "If this was a mistake, reply to this email and a person will help you.\n\n"
            "{{ org_name }}"
        ),
        variables=WITHDRAWN_VARS,
    ),
    DefaultTemplate(
        key=WITHDRAWN,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="سحبت طلبك: {{ job_title }}",
        body=(
            "مرحبا {{ candidate_name }}،\n\n"
            "لقد سحبت طلبك لوظيفة {{ job_title }} لدى {{ org_name }} "
            "(الرقم المرجعي {{ reference }}). لن ننظر فيه بعد الآن.\n\n"
            "إذا حدث ذلك عن طريق الخطأ، رد على هذه الرسالة وسيساعدك أحد أفراد الفريق.\n\n"
            "{{ org_name }}"
        ),
        variables=WITHDRAWN_VARS,
    ),
)

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology

"""Vendor portal email templates, English and Arabic (category ``vendor_comms``).

* ``pay.vendor_portal.login_link``: the magic link, asked for from the portal or sent
  when AP gives a contact access. Works once, expires.
* ``pay.vendor_portal.reply``: AP answered in the portal thread of an invoice. Not
  sensitive by default (``requires_approval`` false): the reply is shown in the portal at
  once. An organisation that saves its own version with ``requires_approval`` makes every
  reply wait for an approval first (the ``pay_message`` graph), like exception messages.

Migration ``pay_0012`` stores them as version 1 shared rows. Copy style: short plain
sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

LOGIN_LINK: Final = "pay.vendor_portal.login_link"
REPLY: Final = "pay.vendor_portal.reply"
CATEGORY: Final = "vendor_comms"


def _v(kind: str, sample: Any, *, required: bool = True) -> dict[str, Any]:
    return {"type": kind, "required": required, "sample": sample}


LOGIN_VARS: Final = {
    "org_name": _v("string", "Acme Trading LLC"),
    "vendor_name": _v("string", "Gulf Office Supplies LLC"),
    "portal_url": _v("url", "https://app.pairly.example/en/vendor-portal/login#sample"),
    "expires_minutes": _v("integer", 30),
    "expires_days": _v("integer", 3, required=False),
}
REPLY_VARS: Final = {
    "org_name": _v("string", "Acme Trading LLC"),
    "vendor_name": _v("string", "Gulf Office Supplies LLC"),
    "invoice_reference": _v("string", "INV-7Q2K9D"),
    "invoice_number": _v("string", "GOS-2026-0412", required=False),
    "message": _v("string", "Thank you. We have the corrected invoice and are checking it."),
    "portal_url": _v("url", "https://app.pairly.example/en/vendor-portal", required=False),
}

PORTAL_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    DefaultTemplate(
        key=LOGIN_LINK,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="Your link to the {{ org_name }} vendor portal",
        body=(
            "Hello,\n\n"
            "Use this link to sign in to the vendor portal of {{ org_name }} for "
            "{{ vendor_name }}. You can follow your invoices, send invoices and credit "
            "notes, and write to accounts payable:\n"
            "{{ portal_url }}\n\n"
            "The link works once and stops working after "
            "{% if expires_days %}{{ expires_days }} days{% else %}"
            "{{ expires_minutes }} minutes{% endif %}. "
            "If you did not ask for it, you can ignore this email."
        ),
        variables=LOGIN_VARS,
    ),
    DefaultTemplate(
        key=LOGIN_LINK,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="رابط بوابة الموردين لدى {{ org_name }}",
        body=(
            "مرحبا،\n\n"
            "استخدم هذا الرابط لتسجيل الدخول إلى بوابة الموردين لدى {{ org_name }} "
            "باسم {{ vendor_name }}. يمكنك متابعة فواتيرك وإرسال الفواتير والإشعارات "
            "الدائنة ومراسلة فريق الحسابات الدائنة:\n"
            "{{ portal_url }}\n\n"
            "يعمل الرابط مرة واحدة ويتوقف بعد "
            "{% if expires_days %}{{ expires_days }} أيام{% else %}"
            "{{ expires_minutes }} دقيقة{% endif %}. "
            "إذا لم تطلبه، يمكنك تجاهل هذه الرسالة."
        ),
        variables=LOGIN_VARS,
    ),
    DefaultTemplate(
        key=REPLY,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="New message about invoice {{ invoice_reference }}",
        body=(
            "Hello {{ vendor_name }},\n\n"
            "Accounts payable at {{ org_name }} wrote about invoice "
            "{{ invoice_reference }}{% if invoice_number %} ({{ invoice_number }}){% endif %}:"
            "\n\n{{ message }}\n\n"
            "{% if portal_url %}Read and answer in the vendor portal: {{ portal_url }}\n\n"
            "{% endif %}{{ org_name }}"
        ),
        variables=REPLY_VARS,
    ),
    DefaultTemplate(
        key=REPLY,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="رسالة جديدة بخصوص الفاتورة {{ invoice_reference }}",
        body=(
            "مرحبا {{ vendor_name }}،\n\n"
            "كتب فريق الحسابات الدائنة لدى {{ org_name }} بخصوص الفاتورة "
            "{{ invoice_reference }}{% if invoice_number %} ({{ invoice_number }}){% endif %}:"
            "\n\n{{ message }}\n\n"
            "{% if portal_url %}اقرأ الرسالة ورد عليها في بوابة الموردين: {{ portal_url }}\n\n"
            "{% endif %}{{ org_name }}"
        ),
        variables=REPLY_VARS,
    ),
)

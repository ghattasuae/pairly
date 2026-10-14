# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""System templates of Pairly Pay, English and Arabic.

``pay.bank_change.alert`` tells the vendor's approvers and the finance controllers that
an invoice check opened (or joined) a bank-detail change ticket and held the vendor's
payments. Category ``urgent``: always sent at once, never held for a digest, and not
something a person can switch off. Migration ``pay_0008`` stores these as version 1
shared rows (``org_id`` NULL); an organisation customises them by saving its own version.

Only the last four characters of the account number ever appear in the message.

Copy style: short plain sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

BANK_CHANGE_ALERT: Final = "pay.bank_change.alert"
CATEGORY: Final = "urgent"


def _v(kind: str, sample: Any, *, required: bool = True) -> dict[str, Any]:
    return {"type": kind, "required": required, "sample": sample}


VARIABLES: Final = {
    "vendor": _v("string", "Gulf Office Supplies LLC"),
    "invoice_reference": _v("string", "INV-7Q2K9D"),
    "ticket_reference": _v("string", "BCT-4M8T2X"),
    "iban_last4": _v("string", "4821"),
    "joined": _v("boolean", False),
    "ticket_url": _v("url", "https://app.pairly.example/pay/bank-changes/sample", required=False),
}
_SHORT: Final = {
    k: VARIABLES[k]
    for k in ("vendor", "invoice_reference", "ticket_reference", "iban_last4", "joined")
}

_EN_BODY = (
    "Invoice {{ invoice_reference }} from {{ vendor }} carries bank details that differ "
    "from the vendor master (account ending {{ iban_last4 }}).\n\n"
    "{% if joined %}Pairly added this request to the open bank-detail change "
    "{{ ticket_reference }}.{% else %}Pairly opened bank-detail change "
    "{{ ticket_reference }}.{% endif %} The vendor's payments are on hold. Nothing is paid "
    "to new details until someone calls the vendor on the number on file and two people "
    "approve the change."
)
_AR_BODY = (
    "الفاتورة {{ invoice_reference }} من {{ vendor }} تحمل بيانات بنكية تختلف عن بيانات "
    "المورد المسجلة (حساب ينتهي بـ {{ iban_last4 }}).\n\n"
    "{% if joined %}أضاف Pairly هذا الطلب إلى طلب تغيير البيانات البنكية المفتوح "
    "{{ ticket_reference }}.{% else %}فتح Pairly طلب تغيير البيانات البنكية "
    "{{ ticket_reference }}.{% endif %} مدفوعات المورد معلقة. لن يُدفع أي مبلغ إلى بيانات "
    "جديدة حتى يتصل أحد بالمورد على الرقم المسجل ويوافق شخصان على التغيير."
)

ALERT_TEMPLATES: Final = (
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="Urgent: payments to {{ vendor }} are on hold after a bank detail change",
        body=_EN_BODY + "{% if ticket_url %}\n\nOpen the change: {{ ticket_url }}{% endif %}",
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="عاجل: مدفوعات {{ vendor }} معلقة بعد تغيير في البيانات البنكية",
        body=_AR_BODY + "{% if ticket_url %}\n\nافتح الطلب: {{ ticket_url }}{% endif %}",
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="slack",
        locale="en",
        category=CATEGORY,
        body="*Urgent: bank detail change for {{ vendor }}.* " + _EN_BODY,
        variables=_SHORT,
    ),
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="slack",
        locale="ar",
        category=CATEGORY,
        body="*عاجل: تغيير البيانات البنكية لـ {{ vendor }}.* " + _AR_BODY,
        variables=_SHORT,
    ),
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="in_app",
        locale="en",
        category=CATEGORY,
        subject="Payments to {{ vendor }} are on hold",
        body=(
            "Invoice {{ invoice_reference }} carries different bank details (account ending "
            "{{ iban_last4 }}). Bank-detail change {{ ticket_reference }} needs a call back "
            "and two approvals."
        ),
        variables=_SHORT,
    ),
    DefaultTemplate(
        key=BANK_CHANGE_ALERT,
        channel="in_app",
        locale="ar",
        category=CATEGORY,
        subject="مدفوعات {{ vendor }} معلقة",
        body=(
            "الفاتورة {{ invoice_reference }} تحمل بيانات بنكية مختلفة (حساب ينتهي بـ "
            "{{ iban_last4 }}). طلب تغيير البيانات البنكية {{ ticket_reference }} يحتاج إلى "
            "اتصال تحقق وموافقتين."
        ),
        variables=_SHORT,
    ),
)

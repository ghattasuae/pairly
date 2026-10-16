# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""System templates of Pairly Pay, English and Arabic.

``pay.bank_change.alert`` tells the vendor's approvers and the finance controllers that
an invoice check opened (or joined) a bank-detail change ticket and held the vendor's
payments. Category ``urgent``: always sent at once, never held for a digest, and not
something a person can switch off. Migration ``pay_0008`` stores these as version 1
shared rows (``org_id`` NULL); an organisation customises them by saving its own version.

``pay.bank_change.portal_alert`` is the same urgent alert, to the same people, when a
vendor's contact asks for new bank details in the vendor portal (no invoice involved).
Migration ``pay_0014`` stores it.

``pay.vendor_portal.appeal`` tells the Pay admins that a vendor's contact asked for a
human review of a decision about its invoice, with the appeal's deadline. Migration
``pay_0018`` stores it.

Only the last four characters of the account number ever appear in the message.

Copy style: short plain sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

BANK_CHANGE_ALERT: Final = "pay.bank_change.alert"
PORTAL_BANK_CHANGE_ALERT: Final = "pay.bank_change.portal_alert"
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

PORTAL_VARIABLES: Final = {
    "vendor": VARIABLES["vendor"],
    "ticket_reference": VARIABLES["ticket_reference"],
    "iban_last4": VARIABLES["iban_last4"],
    "ticket_url": VARIABLES["ticket_url"],
}
_PORTAL_SHORT: Final = {
    k: PORTAL_VARIABLES[k] for k in ("vendor", "ticket_reference", "iban_last4")
}

_PORTAL_EN_BODY = (
    "{{ vendor }} asked in the vendor portal to change its bank details (new account "
    "ending {{ iban_last4 }}).\n\n"
    "Pairly opened bank-detail change {{ ticket_reference }}. The vendor's payments are "
    "on hold. Nothing is paid to new details until someone calls the vendor on the "
    "number on file and two people approve the change."
)
_PORTAL_AR_BODY = (
    "طلب {{ vendor }} عبر بوابة الموردين تغيير بياناته البنكية (حساب جديد ينتهي بـ "
    "{{ iban_last4 }}).\n\n"
    "فتح Pairly طلب تغيير البيانات البنكية {{ ticket_reference }}. مدفوعات المورد معلقة. "
    "لن يُدفع أي مبلغ إلى بيانات جديدة حتى يتصل أحد بالمورد على الرقم المسجل ويوافق "
    "شخصان على التغيير."
)

PORTAL_ALERT_TEMPLATES: Final = (
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="Urgent: payments to {{ vendor }} are on hold after a bank detail change",
        body=_PORTAL_EN_BODY
        + "{% if ticket_url %}\n\nOpen the change: {{ ticket_url }}{% endif %}",
        variables=PORTAL_VARIABLES,
    ),
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="عاجل: مدفوعات {{ vendor }} معلقة بعد تغيير في البيانات البنكية",
        body=_PORTAL_AR_BODY + "{% if ticket_url %}\n\nافتح الطلب: {{ ticket_url }}{% endif %}",
        variables=PORTAL_VARIABLES,
    ),
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="slack",
        locale="en",
        category=CATEGORY,
        body="*Urgent: bank detail change for {{ vendor }}.* " + _PORTAL_EN_BODY,
        variables=_PORTAL_SHORT,
    ),
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="slack",
        locale="ar",
        category=CATEGORY,
        body="*عاجل: تغيير البيانات البنكية لـ {{ vendor }}.* " + _PORTAL_AR_BODY,
        variables=_PORTAL_SHORT,
    ),
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="in_app",
        locale="en",
        category=CATEGORY,
        subject="Payments to {{ vendor }} are on hold",
        body=(
            "{{ vendor }} asked in the vendor portal for new bank details (account ending "
            "{{ iban_last4 }}). Bank-detail change {{ ticket_reference }} needs a call back "
            "and two approvals."
        ),
        variables=_PORTAL_SHORT,
    ),
    DefaultTemplate(
        key=PORTAL_BANK_CHANGE_ALERT,
        channel="in_app",
        locale="ar",
        category=CATEGORY,
        subject="مدفوعات {{ vendor }} معلقة",
        body=(
            "طلب {{ vendor }} عبر بوابة الموردين بيانات بنكية جديدة (حساب ينتهي بـ "
            "{{ iban_last4 }}). طلب تغيير البيانات البنكية {{ ticket_reference }} يحتاج إلى "
            "اتصال تحقق وموافقتين."
        ),
        variables=_PORTAL_SHORT,
    ),
)

# A vendor's contact asked, in the vendor portal, for a person to review a decision about
# one of its invoices (a duplicate flag, a rejection, a bank-detail change step). Sent to
# the organisation's Pay admins with the deadline of the appeal (its SLA), category
# ``data_requests`` (always urgent, never switched off), like a candidate's request for a
# human review. Migration ``pay_0018`` stores it. Ids and references only, never the
# vendor's words.
VENDOR_APPEAL: Final = "pay.vendor_portal.appeal"
APPEAL_CATEGORY: Final = "data_requests"
APPEAL_VARIABLES: Final = {
    "vendor": _v("string", "Gulf Office Supplies LLC"),
    "invoice_reference": _v("string", "INV-7Q2K9D"),
    "appeal_ref": _v("string", "APL-3F2A9C1D"),
    "decision": _v("string", "rejection"),
    "due_at": _v("string", "2026-10-30"),
    "appeal_url": _v("url", "https://app.pairly.example/decisions/sample", required=False),
}
_APPEAL_SHORT: Final = {k: APPEAL_VARIABLES[k] for k in APPEAL_VARIABLES if k != "appeal_url"}
_DECISION_EN: Final = (
    '{% if decision == "duplicate_flag" %}the duplicate flag on invoice '
    '{{ invoice_reference }}{% elif decision == "rejection" %}the rejection of invoice '
    "{{ invoice_reference }}{% else %}a bank-detail change step linked to invoice "
    "{{ invoice_reference }}{% endif %}"
)
_DECISION_AR: Final = (
    '{% if decision == "duplicate_flag" %}علامة التكرار على الفاتورة '
    '{{ invoice_reference }}{% elif decision == "rejection" %}رفض الفاتورة '
    "{{ invoice_reference }}{% else %}خطوة في تغيير البيانات البنكية مرتبطة بالفاتورة "
    "{{ invoice_reference }}{% endif %}"
)
_APPEAL_EN_BODY: Final = (
    "{{ vendor }} asked in the vendor portal for a person to review "
    + _DECISION_EN
    + ".\n\nAppeal {{ appeal_ref }}. Write the outcome by {{ due_at }}."
)
_APPEAL_AR_BODY: Final = (
    "طلب {{ vendor }} عبر بوابة الموردين أن يراجع شخص "
    + _DECISION_AR
    + ".\n\nالطعن {{ appeal_ref }}. اكتب القرار قبل {{ due_at }}."
)

APPEAL_TEMPLATES: Final = (
    DefaultTemplate(
        key=VENDOR_APPEAL,
        channel="email",
        locale="en",
        category=APPEAL_CATEGORY,
        subject="Vendor appeal {{ appeal_ref }}: {{ vendor }} asked for a human review",
        body=_APPEAL_EN_BODY
        + "{% if appeal_url %}\n\nOpen the decision: {{ appeal_url }}{% endif %}",
        variables=APPEAL_VARIABLES,
    ),
    DefaultTemplate(
        key=VENDOR_APPEAL,
        channel="email",
        locale="ar",
        category=APPEAL_CATEGORY,
        subject="طعن من مورد {{ appeal_ref }}: طلب {{ vendor }} مراجعة بشرية",
        body=_APPEAL_AR_BODY + "{% if appeal_url %}\n\nافتح القرار: {{ appeal_url }}{% endif %}",
        variables=APPEAL_VARIABLES,
    ),
    DefaultTemplate(
        key=VENDOR_APPEAL,
        channel="in_app",
        locale="en",
        category=APPEAL_CATEGORY,
        subject="{{ vendor }} asked for a human review",
        body=_APPEAL_EN_BODY,
        variables=_APPEAL_SHORT,
    ),
    DefaultTemplate(
        key=VENDOR_APPEAL,
        channel="in_app",
        locale="ar",
        category=APPEAL_CATEGORY,
        subject="طلب {{ vendor }} مراجعة بشرية",
        body=_APPEAL_AR_BODY,
        variables=_APPEAL_SHORT,
    ),
)

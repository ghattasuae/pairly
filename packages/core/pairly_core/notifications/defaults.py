# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""System default templates, English and Arabic.

Migration 0011 seeds these as version 1 with ``org_id`` NULL (shared, read only). An
organisation customises a template by saving its own version of the same key, channel
and locale; its latest version wins over the system default. Changing a default here
needs a new migration that inserts the next version (versions are immutable).

The sensitive templates (``requires_approval``) are the messages a human must approve
before they go out (brief 3.5.2): candidate rejections and offers, and messages to
vendors. Their keys sit in the workflow namespaces; core only stores them as data.

Copy style: short plain sentences, never an em dash (the lint refuses them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class DefaultTemplate:
    key: str
    channel: str
    locale: str
    category: str
    body: str
    subject: str | None = None
    body_html: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False


def _v(kind: str, sample: Any = None, *, required: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": required}
    if sample is not None:
        data["sample"] = sample
    return data


_APPROVAL_VARS: Final = {
    "title": _v("string", "Invoice INV-1042 from Example Supplies, 1250.00 AED"),
    "step": _v("string", "manager"),
    "due_at": _v("datetime", "2026-10-08T09:00:00+00:00"),
    "approve_url": _v("url", "https://app.pairly.example/approvals/link#sample", required=False),
    "reject_url": _v("url", "https://app.pairly.example/approvals/link#sample", required=False),
    "inbox_url": _v("url", "https://app.pairly.example/approvals"),
    "expires_minutes": _v("integer", 30, required=False),
}
_INAPP_APPROVAL_VARS: Final = {k: _APPROVAL_VARS[k] for k in ("title", "step", "due_at")}
_SLACK_APPROVAL_VARS: Final = {k: _APPROVAL_VARS[k] for k in ("title", "step", "due_at")}

_BUTTON = (
    '<p><a href="{url}" style="display:inline-block;padding:10px 16px;border-radius:8px;'
    'background:{bg};color:#ffffff;text-decoration:none">{label}</a></p>'
)

APPROVAL_REQUESTED: Final = (
    DefaultTemplate(
        key="core.approval.requested",
        channel="email",
        locale="en",
        category="approvals",
        subject="Approval needed: {{ title }}",
        body=(
            "An item is waiting for your decision.\n\n"
            "{{ title }}\nStep: {{ step }}\nDue: {{ due_at }}\n"
            "{% if approve_url %}\nApprove: {{ approve_url }}\nReject: {{ reject_url }}\n\n"
            "These links work once, only for you, and expire in {{ expires_minutes }} "
            "minutes. You confirm on the next page after signing in with two-factor "
            "authentication.\n{% endif %}\nAll items: {{ inbox_url }}"
        ),
        body_html=(
            "<p>An item is waiting for your decision.</p>"
            "<p><strong>{{ title }}</strong><br>Step: {{ step }}<br>Due: {{ due_at }}</p>"
            + "{% if approve_url %}"
            + _BUTTON.format(url="{{ approve_url }}", bg="#1d5c52", label="Approve")
            + _BUTTON.format(url="{{ reject_url }}", bg="#8a2b2b", label="Reject")
            + '<p style="color:#5b6766;font-size:14px">These links work once, only for you, '
            "and expire in {{ expires_minutes }} minutes. You confirm on the next page after "
            "signing in with two-factor authentication.</p>{% endif %}"
            '<p><a href="{{ inbox_url }}">Open the approval inbox</a></p>'
        ),
        variables=_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.requested",
        channel="email",
        locale="ar",
        category="approvals",
        subject="مطلوب موافقتك: {{ title }}",
        body=(
            "يوجد عنصر بانتظار قرارك.\n\n"
            "{{ title }}\nالمرحلة: {{ step }}\nالموعد النهائي: {{ due_at }}\n"
            "{% if approve_url %}\nموافقة: {{ approve_url }}\nرفض: {{ reject_url }}\n\n"
            "هذه الروابط تعمل مرة واحدة ولك وحدك، وتنتهي صلاحيتها بعد {{ expires_minutes }} "
            "دقيقة. ستؤكد قرارك في الصفحة التالية بعد تسجيل الدخول بالتحقق الثنائي.\n"
            "{% endif %}\nكل العناصر: {{ inbox_url }}"
        ),
        body_html=(
            "<p>يوجد عنصر بانتظار قرارك.</p>"
            "<p><strong>{{ title }}</strong><br>المرحلة: {{ step }}<br>"
            "الموعد النهائي: {{ due_at }}</p>"
            + "{% if approve_url %}"
            + _BUTTON.format(url="{{ approve_url }}", bg="#1d5c52", label="موافقة")
            + _BUTTON.format(url="{{ reject_url }}", bg="#8a2b2b", label="رفض")
            + '<p style="color:#5b6766;font-size:14px">هذه الروابط تعمل مرة واحدة ولك وحدك، '
            "وتنتهي صلاحيتها بعد {{ expires_minutes }} دقيقة. ستؤكد قرارك في الصفحة التالية "
            "بعد تسجيل الدخول بالتحقق الثنائي.</p>{% endif %}"
            '<p><a href="{{ inbox_url }}">افتح صندوق الموافقات</a></p>'
        ),
        variables=_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.requested",
        channel="slack",
        locale="en",
        category="approvals",
        body="*Approval needed:* {{ title }}\nStep: {{ step }}. Due: {{ due_at }}.",
        variables=_SLACK_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.requested",
        channel="slack",
        locale="ar",
        category="approvals",
        body="*مطلوب موافقتك:* {{ title }}\nالمرحلة: {{ step }}. الموعد النهائي: {{ due_at }}.",
        variables=_SLACK_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.requested",
        channel="in_app",
        locale="en",
        category="approvals",
        subject="Approval needed: {{ title }}",
        body="Step {{ step }}. Due {{ due_at }}.",
        variables=_INAPP_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.requested",
        channel="in_app",
        locale="ar",
        category="approvals",
        subject="مطلوب موافقتك: {{ title }}",
        body="المرحلة {{ step }}. الموعد النهائي {{ due_at }}.",
        variables=_INAPP_APPROVAL_VARS,
    ),
)

APPROVAL_ESCALATED: Final = (
    DefaultTemplate(
        key="core.approval.escalated",
        channel="email",
        locale="en",
        category="sla",
        subject="Overdue approval: {{ title }}",
        body=(
            "This approval is past its deadline and needs a decision now.\n\n"
            "{{ title }}\nStep: {{ step }}\nWas due: {{ due_at }}\n"
            "{% if approve_url %}\nApprove: {{ approve_url }}\nReject: {{ reject_url }}\n\n"
            "These links work once, only for you, and expire in {{ expires_minutes }} "
            "minutes.\n{% endif %}\nAll items: {{ inbox_url }}"
        ),
        variables=_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.escalated",
        channel="email",
        locale="ar",
        category="sla",
        subject="موافقة متأخرة: {{ title }}",
        body=(
            "تجاوزت هذه الموافقة موعدها النهائي وتحتاج إلى قرار الآن.\n\n"
            "{{ title }}\nالمرحلة: {{ step }}\nكان الموعد: {{ due_at }}\n"
            "{% if approve_url %}\nموافقة: {{ approve_url }}\nرفض: {{ reject_url }}\n\n"
            "هذه الروابط تعمل مرة واحدة ولك وحدك، وتنتهي صلاحيتها بعد {{ expires_minutes }} "
            "دقيقة.\n{% endif %}\nكل العناصر: {{ inbox_url }}"
        ),
        variables=_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.escalated",
        channel="slack",
        locale="en",
        category="sla",
        body="*Overdue approval:* {{ title }}\nStep: {{ step }}. Was due: {{ due_at }}.",
        variables=_SLACK_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.escalated",
        channel="slack",
        locale="ar",
        category="sla",
        body="*موافقة متأخرة:* {{ title }}\nالمرحلة: {{ step }}. كان الموعد: {{ due_at }}.",
        variables=_SLACK_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.escalated",
        channel="in_app",
        locale="en",
        category="sla",
        subject="Overdue approval: {{ title }}",
        body="Step {{ step }} was due {{ due_at }}.",
        variables=_INAPP_APPROVAL_VARS,
    ),
    DefaultTemplate(
        key="core.approval.escalated",
        channel="in_app",
        locale="ar",
        category="sla",
        subject="موافقة متأخرة: {{ title }}",
        body="كان موعد المرحلة {{ step }} في {{ due_at }}.",
        variables=_INAPP_APPROVAL_VARS,
    ),
)

_REMINDER_VARS: Final = {
    "title": _APPROVAL_VARS["title"],
    "inbox_url": _APPROVAL_VARS["inbox_url"],
}
APPROVAL_REMINDER: Final = (
    DefaultTemplate(
        key="core.approval.reminder",
        channel="slack",
        locale="en",
        category="reminders",
        body="Reminder: {{ title }} is still waiting for your decision.\n{{ inbox_url }}",
        variables=_REMINDER_VARS,
    ),
    DefaultTemplate(
        key="core.approval.reminder",
        channel="slack",
        locale="ar",
        category="reminders",
        body="تذكير: {{ title }} ما زال بانتظار قرارك.\n{{ inbox_url }}",
        variables=_REMINDER_VARS,
    ),
    DefaultTemplate(
        key="core.approval.reminder",
        channel="in_app",
        locale="en",
        category="reminders",
        subject="Reminder: {{ title }}",
        body="Still waiting for your decision.",
        variables={"title": _APPROVAL_VARS["title"]},
    ),
    DefaultTemplate(
        key="core.approval.reminder",
        channel="in_app",
        locale="ar",
        category="reminders",
        subject="تذكير: {{ title }}",
        body="ما زال بانتظار قرارك.",
        variables={"title": _APPROVAL_VARS["title"]},
    ),
)

_RECEIPT_VARS: Final = {
    "reference": _v("string", "REF-3F2A9C1B0D"),
    "original_subject": _v("string", "Invoice INV-1042", required=False),
    "received_at": _v("datetime", "2026-10-07T09:30:00+00:00"),
    "document_count": _v("integer", 1),
}
RECEIPT: Final = (
    DefaultTemplate(
        key="core.ingest.receipt",
        channel="email",
        locale="en",
        category="receipts",
        subject="Received: {{ reference }}",
        body=(
            "We received your email{% if original_subject %} "
            '"{{ original_subject }}"{% endif %} on {{ received_at }}.\n\n'
            "Reference: {{ reference }}\nFiles received: {{ document_count }}\n\n"
            "Please quote the reference if you contact us about it. This is an automatic "
            "reply, so there is no need to answer it."
        ),
        variables=_RECEIPT_VARS,
    ),
    DefaultTemplate(
        key="core.ingest.receipt",
        channel="email",
        locale="ar",
        category="receipts",
        subject="تم الاستلام: {{ reference }}",
        body=(
            "استلمنا رسالتك{% if original_subject %} "
            '"{{ original_subject }}"{% endif %} في {{ received_at }}.\n\n'
            "الرقم المرجعي: {{ reference }}\nعدد الملفات المستلمة: {{ document_count }}\n\n"
            "يرجى ذكر الرقم المرجعي عند التواصل معنا بشأنها. هذا رد تلقائي، فلا حاجة للرد عليه."
        ),
        variables=_RECEIPT_VARS,
    ),
)

_DIGEST_VARS: Final = {
    "items": _v(
        "list",
        [
            {
                "title": "Approval needed: Invoice INV-1042",
                "url": "https://app.pairly.example/approvals",
            }
        ],
    ),
    "count": _v("integer", 1),
    "date": _v("string", "2026-10-07"),
}
DIGEST: Final = (
    DefaultTemplate(
        key="core.digest.daily",
        channel="email",
        locale="en",
        category="digest",
        subject="Your Pairly summary for {{ date }}: {{ count }} items",
        body=(
            "Here is what happened since your last summary.\n"
            "{% for item in items %}\n- {{ item.title }}{% if item.url %}: {{ item.url }}"
            "{% endif %}{% endfor %}"
        ),
        variables=_DIGEST_VARS,
    ),
    DefaultTemplate(
        key="core.digest.daily",
        channel="email",
        locale="ar",
        category="digest",
        subject="ملخص Pairly ليوم {{ date }}: {{ count }} عناصر",
        body=(
            "هذا ما حدث منذ آخر ملخص.\n"
            "{% for item in items %}\n- {{ item.title }}{% if item.url %}: {{ item.url }}"
            "{% endif %}{% endfor %}"
        ),
        variables=_DIGEST_VARS,
    ),
)

_CANDIDATE_VARS: Final = {
    "candidate_name": _v("string", "Sam Example"),
    "job_title": _v("string", "Accounts Payable Specialist"),
    "company": _v("string", "Example Trading LLC"),
    "reason": _v("text", required=False),
}
_OFFER_VARS: Final = {
    "candidate_name": _v("string", "Sam Example"),
    "job_title": _v("string", "Accounts Payable Specialist"),
    "company": _v("string", "Example Trading LLC"),
    "offer_url": _v("url", "https://app.pairly.example/offers/sample"),
}
_VENDOR_VARS: Final = {
    "vendor_name": _v("string", "Example Supplies"),
    "company": _v("string", "Example Trading LLC"),
    "message": _v("text", "We need a corrected invoice with the purchase order number."),
}
SENSITIVE: Final = (
    DefaultTemplate(
        key="hire.candidate.rejection",
        channel="email",
        locale="en",
        category="candidate_comms",
        subject="Your application for {{ job_title }}",
        body=(
            "Dear {{ candidate_name }},\n\nThank you for applying for {{ job_title }} at "
            "{{ company }}. We have decided not to move forward with your application."
            "{% if reason %}\n\n{{ reason }}{% endif %}\n\n"
            "We are grateful for the time you gave us and wish you well.\n\n{{ company }}"
        ),
        variables=_CANDIDATE_VARS,
        requires_approval=True,
    ),
    DefaultTemplate(
        key="hire.candidate.rejection",
        channel="email",
        locale="ar",
        category="candidate_comms",
        subject="طلبك لوظيفة {{ job_title }}",
        body=(
            "عزيزنا {{ candidate_name }}،\n\nشكرًا لتقدمك لوظيفة {{ job_title }} لدى "
            "{{ company }}. قررنا عدم المضي في طلبك.{% if reason %}\n\n{{ reason }}{% endif %}"
            "\n\nنقدّر الوقت الذي منحتنا إياه ونتمنى لك التوفيق.\n\n{{ company }}"
        ),
        variables=_CANDIDATE_VARS,
        requires_approval=True,
    ),
    DefaultTemplate(
        key="hire.candidate.offer",
        channel="email",
        locale="en",
        category="candidate_comms",
        subject="Your offer from {{ company }}",
        body=(
            "Dear {{ candidate_name }},\n\nWe are pleased to offer you the role of "
            "{{ job_title }} at {{ company }}. You can read and answer the offer here:\n"
            "{{ offer_url }}\n\n{{ company }}"
        ),
        variables=_OFFER_VARS,
        requires_approval=True,
    ),
    DefaultTemplate(
        key="hire.candidate.offer",
        channel="email",
        locale="ar",
        category="candidate_comms",
        subject="عرض العمل من {{ company }}",
        body=(
            "عزيزنا {{ candidate_name }}،\n\nيسعدنا أن نعرض عليك وظيفة {{ job_title }} لدى "
            "{{ company }}. يمكنك قراءة العرض والرد عليه هنا:\n{{ offer_url }}\n\n{{ company }}"
        ),
        variables=_OFFER_VARS,
        requires_approval=True,
    ),
    DefaultTemplate(
        key="pay.vendor.message",
        channel="email",
        locale="en",
        category="vendor_comms",
        subject="A message from {{ company }}",
        body="Dear {{ vendor_name }},\n\n{{ message }}\n\n{{ company }}",
        variables=_VENDOR_VARS,
        requires_approval=True,
    ),
    DefaultTemplate(
        key="pay.vendor.message",
        channel="email",
        locale="ar",
        category="vendor_comms",
        subject="رسالة من {{ company }}",
        body="عزيزنا {{ vendor_name }}،\n\n{{ message }}\n\n{{ company }}",
        variables=_VENDOR_VARS,
        requires_approval=True,
    ),
)

SYSTEM_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    *APPROVAL_REQUESTED,
    *APPROVAL_ESCALATED,
    *APPROVAL_REMINDER,
    *RECEIPT,
    *DIGEST,
    *SENSITIVE,
)

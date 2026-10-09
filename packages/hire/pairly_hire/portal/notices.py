# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Who hears about what a candidate does in the portal, and the candidate about file
requests (WP2.9).

* A new data request (an export, a deletion request, a human review request) tells the
  organisation's Hire admins, in the app and by email, in the urgent ``data_requests``
  category (core, always urgent, not switchable), with the deadline to act by
  (``SLA_DAYS``: 30 days for export and deletion, the one-month data protection limit;
  7 days for a human review). The notice names the kind, the application reference and
  the deadline, never the candidate's name or their note.
* A candidate's reply or uploaded file tells the job's recruiter (the person who created
  the job; for a file, also the person who asked for it), in the app and by email in the
  ``updates`` category, which waits for the daily digest by default.
* A file the hiring team asks for is emailed to the candidate (``candidate_comms``,
  transactional) with the link to the portal.

All in the caller's transaction, so a notice exists only if the request does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.models import Membership, Role
from pairly_core.notifications.defaults import DefaultTemplate
from pairly_core.notifications.models import Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import BuiltinRole

DATA_REQUEST: Final = "hire.portal.data_request"
CANDIDATE_ACTIVITY: Final = "hire.portal.candidate_activity"
FILE_REQUESTED: Final = "hire.portal.file_requested"
WORKFLOW: Final = "hire"
SLA_DAYS: Final = {"export": 30, "deletion": 30, "human_review": 7}


def _v(kind: str, sample: Any) -> dict[str, Any]:
    return {"type": kind, "required": True, "sample": sample}


_REQUEST_VARS: Final = {
    "kind": _v("string", "deletion"),
    "reference": _v("string", "7KQ2-M9XD"),
    "due_at": _v("string", "2026-11-08"),
}
_ACTIVITY_VARS: Final = {
    "activity": _v("string", "reply"),
    "label": _v("string", "Candidate 7KQ2-M9XD"),
    "job_title": _v("string", "Senior Backend Engineer"),
}
_FILE_VARS: Final = {
    "org_name": _v("string", "Acme Logistics"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "label": _v("string", "Portfolio"),
    "portal_url": _v("url", "https://app.pairly.example/en/portal/login"),
}


def _pair(
    key: str, category: str, channel: str, vars_: Any, en: tuple[str, str], ar: tuple[str, str]
) -> tuple[DefaultTemplate, DefaultTemplate]:
    return (
        DefaultTemplate(
            key=key,
            channel=channel,
            locale="en",
            category=category,
            subject=en[0],
            body=en[1],
            variables=vars_,
        ),
        DefaultTemplate(
            key=key,
            channel=channel,
            locale="ar",
            category=category,
            subject=ar[0],
            body=ar[1],
            variables=vars_,
        ),
    )


_KIND_EN: Final = (
    '{% if kind == "export" %}downloaded their data (already delivered; check nothing '
    'else is owed){% elif kind == "human_review" %}asked a person to review an '
    "AI-assisted step{% else %}asked for their data to be deleted{% endif %}"
)
_KIND_AR: Final = (
    '{% if kind == "export" %}نزّل بياناته (تم التسليم؛ تحقق من عدم وجود طلبات أخرى)'
    '{% elif kind == "human_review" %}طلب مراجعة بشرية لخطوة بمساعدة الذكاء الاصطناعي'
    "{% else %}طلب حذف بياناته{% endif %}"
)
_ACT_WHAT_EN: Final = (
    '{% if activity == "file" %}uploaded a file you asked for'
    "{% else %}replied to a message{% endif %}"
)
_ACT_WHAT_AR: Final = '{% if activity == "file" %}رفع ملفًا طلبته{% else %}رد على رسالة{% endif %}'
_REQ_EN = (
    "Candidate data request: {{ reference }}",
    "A candidate (application {{ reference }}) "
    + _KIND_EN
    + ".\n\nAct by {{ due_at }}. Open Pairly Hire to handle it.",
)
_REQ_AR = (
    "طلب بيانات من مرشح: {{ reference }}",
    "مرشح (الطلب {{ reference }}) "
    + _KIND_AR
    + ".\n\nيجب التصرف قبل {{ due_at }}. افتح Pairly Hire لمعالجته.",
)
_ACT_EN = ("{{ label }} " + _ACT_WHAT_EN, "{{ label }} " + _ACT_WHAT_EN + " ({{ job_title }}).")
_ACT_AR = ("{{ label }} " + _ACT_WHAT_AR, "{{ label }} " + _ACT_WHAT_AR + " ({{ job_title }}).")
_FILE_EN = (
    "{{ org_name }} asked you for a file: {{ label }}",
    "Hello,\n\nThe hiring team for {{ job_title }} at {{ org_name }} asked you for a file: "
    "{{ label }}.\n\nSign in to your candidate portal to upload it:\n{{ portal_url }}\n\n"
    "{{ org_name }}",
)
_FILE_AR = (
    "طلبت {{ org_name }} منك ملفًا: {{ label }}",
    "مرحبا،\n\nطلب فريق التوظيف لوظيفة {{ job_title }} لدى {{ org_name }} منك ملفًا: "
    "{{ label }}.\n\nسجّل الدخول إلى بوابة المرشحين لرفعه:\n{{ portal_url }}\n\n"
    "{{ org_name }}",
)

NOTICE_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    *_pair(DATA_REQUEST, "data_requests", "email", _REQUEST_VARS, _REQ_EN, _REQ_AR),
    *_pair(DATA_REQUEST, "data_requests", "in_app", _REQUEST_VARS, _REQ_EN, _REQ_AR),
    *_pair(CANDIDATE_ACTIVITY, "updates", "email", _ACTIVITY_VARS, _ACT_EN, _ACT_AR),
    *_pair(CANDIDATE_ACTIVITY, "updates", "in_app", _ACTIVITY_VARS, _ACT_EN, _ACT_AR),
    *_pair(FILE_REQUESTED, "candidate_comms", "email", _FILE_VARS, _FILE_EN, _FILE_AR),
)


async def hire_admins(session: AsyncSession) -> list[uuid.UUID]:
    """Members with the admin role for Hire (organisation-wide or Hire only)."""
    rows = await session.scalars(
        select(Membership.user_id)
        .join(Role, Role.id == Membership.role_id)
        .where(
            Role.key == BuiltinRole.ADMIN.value,
            or_(Membership.workflow.is_(None), Membership.workflow == WORKFLOW),
        )
        .distinct()
    )
    return sorted(set(rows), key=str)


def due_date(kind: str, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(days=SLA_DAYS[kind])


async def notify_data_request(
    session: AsyncSession,
    *,
    kind: str,
    request_id: str,
    reference: str,
    now: datetime | None = None,
) -> datetime:
    """Tell every Hire admin (in the app and by email, urgent). Returns the deadline."""
    due = due_date(kind, now)
    for admin in await hire_admins(session):
        await notify(
            session,
            recipient=Recipient.user(admin),
            template_key=DATA_REQUEST,
            variables={
                "kind": kind,
                "reference": reference,
                "due_at": due.date().isoformat(),
            },
            dedupe_key=f"hire.data_request:{kind}:{request_id}:{admin}",
            channels=["in_app", "email"],
            urgency=Urgency.URGENT,
            extras=Extras(link="/hire", extra={"kind": kind, "due_at": due.isoformat()}),
            workflow=WORKFLOW,
        )
    return due


async def notify_recruiters(
    session: AsyncSession,
    *,
    recruiters: set[uuid.UUID],
    activity: str,
    event_id: str,
    label: str,
    job_title: str,
    link: str,
) -> None:
    """Tell the job's recruiters about a reply or a file (digest-friendly ``updates``)."""
    for user_id in sorted(recruiters, key=str):
        await notify(
            session,
            recipient=Recipient.user(user_id),
            template_key=CANDIDATE_ACTIVITY,
            variables={
                "activity": activity,
                "label": label,
                "job_title": job_title,
            },
            dedupe_key=f"hire.candidate_activity:{activity}:{event_id}:{user_id}",
            channels=["in_app", "email"],
            extras=Extras(link=link),
            workflow=WORKFLOW,
        )


async def notify_file_requested(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    email: str,
    locale: str,
    org_name: str,
    job_title: str,
    label: str,
    portal_url: str,
) -> None:
    await notify(
        session,
        recipient=Recipient.external(email, locale=locale),
        template_key=FILE_REQUESTED,
        variables={
            "org_name": org_name,
            "job_title": job_title,
            "label": label,
            "portal_url": portal_url,
        },
        dedupe_key=f"hire.file_requested:{request_id}",
        channels=["email"],
        workflow=WORKFLOW,
    )

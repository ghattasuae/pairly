# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""Scheduling messages, English and Arabic (migration ``hire_0005`` seeds them).

Candidate email (category ``interviews``, transactional: always sent):

* ``hire.candidate.interview_confirmed``: a time is booked (by the candidate or the
  team), with the calendar file and the link to change or cancel it.
* ``hire.candidate.interview_cancelled``: the interview is cancelled (calendar file with
  ``METHOD:CANCEL``).
* The invitation with a self-scheduling link, a move by the team and the reminders use
  the messaging library's ``interview_invite``, ``interview_reschedule`` and
  ``interview_reminder``.

Interviewer email (category ``interviews``):

* ``hire.interviewer.briefing``: you are on the panel; when, where, a short candidate
  summary that respects blind mode, and the interview kit (a placeholder until M3).
* ``hire.interviewer.rescheduled``, ``hire.interviewer.cancelled``,
  ``hire.interviewer.reminder`` (also in the app: an interview starting soon is urgent).

``interview_details`` is text written by ``pairly_hire.scheduling.emails`` in the
candidate's language and time zone; interviewer messages use ``starts_at`` (a datetime the
renderer shows in each reader's own time zone).

Copy style: short plain sentences, never an em dash (the tests lint every template).
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

CONFIRMED: Final = "hire.candidate.interview_confirmed"
CANCELLED: Final = "hire.candidate.interview_cancelled"
BRIEFING: Final = "hire.interviewer.briefing"
PANEL_RESCHEDULED: Final = "hire.interviewer.rescheduled"
PANEL_CANCELLED: Final = "hire.interviewer.cancelled"
PANEL_REMINDER: Final = "hire.interviewer.reminder"


def _v(kind: str, sample: Any = None, *, required: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": required}
    if sample is not None:
        data["sample"] = sample
    return data


_DETAILS = "Tuesday 13 October 2026, 10:00 to 10:45 (Asia/Dubai). A 45 minute video call."
_CANDIDATE: Final = {
    "candidate_name": _v("string", "Layla Haddad"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "company": _v("string", "Acme Logistics"),
    "interview_details": _v("text", _DETAILS),
}
_CONFIRMED_VARS: Final = _CANDIDATE | {
    "manage_url": _v("url", "https://app.pairly.example/en/schedule#sample", required=False),
}
_PANEL: Final = {
    "candidate_label": _v("string", "Candidate 7KQ2-M9XD"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "starts_at": _v("datetime", "2026-10-13T06:00:00+00:00"),
    "duration_min": _v("integer", 45),
    "meeting_url": _v("url", "https://meet.pairly.example/abc-defg-hij", required=False),
    "interview_url": _v("url", "https://app.pairly.example/en/hire/jobs/sample", required=False),
}
_BRIEFING_VARS: Final = _PANEL | {
    "summary": _v("text", "Stage: Interview. Screening: strong match on 3 of 4 criteria."),
    "panel": _v("string", "Omar, Sara"),
}
_CANCELLED_PANEL_VARS: Final = {
    k: _PANEL[k] for k in ("candidate_label", "job_title", "starts_at", "interview_url")
}
_INAPP_REMINDER_VARS: Final = {
    k: _PANEL[k] for k in ("candidate_label", "job_title", "starts_at", "duration_min")
}

_JOIN_EN = "{% if meeting_url %}\nJoin the call: {{ meeting_url }}{% endif %}"
_JOIN_AR = "{% if meeting_url %}\nرابط المكالمة: {{ meeting_url }}{% endif %}"
_OPEN_EN = "{% if interview_url %}\n\nOpen the candidate in Pairly: {{ interview_url }}{% endif %}"
_OPEN_AR = "{% if interview_url %}\n\nافتح ملف المرشح في Pairly: {{ interview_url }}{% endif %}"


def _t(
    key: str,
    locale: str,
    subject: str,
    body: str,
    variables: dict[str, Any],
    *,
    channel: str = "email",
) -> DefaultTemplate:
    return DefaultTemplate(
        key=key,
        channel=channel,
        locale=locale,
        category="interviews",
        subject=subject,
        body=body,
        variables=variables,
    )


SCHEDULING_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    # ------------------------------------------------------------ candidate
    _t(
        CONFIRMED,
        "en",
        "Your interview is booked: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "Your interview for {{ job_title }} at {{ company }} is booked.\n\n"
        "{{ interview_details }}\n\n"
        "The calendar invitation is attached."
        "{% if manage_url %}\n\nNeed another time, or cannot come? Change or cancel here: "
        "{{ manage_url }}{% endif %}"
        "\n\nReply to this email if you need any adjustment for the interview.\n\n"
        "{{ company }}",
        _CONFIRMED_VARS,
    ),
    _t(
        CONFIRMED,
        "ar",
        "تم حجز مقابلتك: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "تم حجز مقابلتك لوظيفة {{ job_title }} لدى {{ company }}.\n\n"
        "{{ interview_details }}\n\n"
        "دعوة التقويم مرفقة."
        "{% if manage_url %}\n\nتحتاج إلى موعد آخر، أو لا تستطيع الحضور؟ غير الموعد أو ألغه "
        "من هنا: {{ manage_url }}{% endif %}"
        "\n\nرد على هذه الرسالة إذا احتجت إلى أي تعديل للمقابلة.\n\n{{ company }}",
        _CONFIRMED_VARS,
    ),
    _t(
        CANCELLED,
        "en",
        "Your interview is cancelled: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "Your interview for {{ job_title }} at {{ company }} is cancelled.\n\n"
        "{{ interview_details }}\n\n"
        "The attached calendar file removes it from your calendar. We will write to you "
        "about the next step.\n\n{{ company }}",
        _CANDIDATE,
    ),
    _t(
        CANCELLED,
        "ar",
        "تم إلغاء مقابلتك: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "تم إلغاء مقابلتك لوظيفة {{ job_title }} لدى {{ company }}.\n\n"
        "{{ interview_details }}\n\n"
        "ملف التقويم المرفق يزيلها من تقويمك. سنكتب لك بشأن الخطوة التالية.\n\n{{ company }}",
        _CANDIDATE,
    ),
    # ------------------------------------------------------------ interviewers
    _t(
        BRIEFING,
        "en",
        "Interview: {{ candidate_label }}, {{ job_title }}",
        "You are on the interview panel for {{ job_title }}.\n\n"
        "When: {{ starts_at }}, {{ duration_min }} minutes." + _JOIN_EN + "\n"
        "Panel: {{ panel }}\n\n"
        "Candidate: {{ candidate_label }}\n{{ summary }}\n\n"
        "Interview kit: the questions for this stage will be here once interview kits "
        "are available. Until then, use the job's rubric." + _OPEN_EN,
        _BRIEFING_VARS,
    ),
    _t(
        BRIEFING,
        "ar",
        "مقابلة: {{ candidate_label }}، {{ job_title }}",
        "أنت ضمن لجنة المقابلة لوظيفة {{ job_title }}.\n\n"
        "الموعد: {{ starts_at }}، لمدة {{ duration_min }} دقيقة." + _JOIN_AR + "\n"
        "اللجنة: {{ panel }}\n\n"
        "المرشح: {{ candidate_label }}\n{{ summary }}\n\n"
        "دليل المقابلة: ستظهر أسئلة هذه المرحلة هنا عندما تتوفر أدلة المقابلات. حتى ذلك "
        "الحين، استخدم معايير التقييم الخاصة بالوظيفة." + _OPEN_AR,
        _BRIEFING_VARS,
    ),
    _t(
        PANEL_RESCHEDULED,
        "en",
        "New time: interview with {{ candidate_label }}",
        "The interview with {{ candidate_label }} for {{ job_title }} has a new time.\n\n"
        "When: {{ starts_at }}, {{ duration_min }} minutes." + _JOIN_EN + "\n\n"
        "The updated calendar invitation is attached." + _OPEN_EN,
        _PANEL,
    ),
    _t(
        PANEL_RESCHEDULED,
        "ar",
        "موعد جديد: مقابلة {{ candidate_label }}",
        "تغير موعد المقابلة مع {{ candidate_label }} لوظيفة {{ job_title }}.\n\n"
        "الموعد: {{ starts_at }}، لمدة {{ duration_min }} دقيقة." + _JOIN_AR + "\n\n"
        "دعوة التقويم المحدثة مرفقة." + _OPEN_AR,
        _PANEL,
    ),
    _t(
        PANEL_CANCELLED,
        "en",
        "Cancelled: interview with {{ candidate_label }}",
        "The interview with {{ candidate_label }} for {{ job_title }} on {{ starts_at }} "
        "is cancelled. The attached calendar file removes it from your calendar." + _OPEN_EN,
        _CANCELLED_PANEL_VARS,
    ),
    _t(
        PANEL_CANCELLED,
        "ar",
        "ألغيت: مقابلة {{ candidate_label }}",
        "ألغيت المقابلة مع {{ candidate_label }} لوظيفة {{ job_title }} في {{ starts_at }}. "
        "ملف التقويم المرفق يزيلها من تقويمك." + _OPEN_AR,
        _CANCELLED_PANEL_VARS,
    ),
    _t(
        PANEL_REMINDER,
        "en",
        "Reminder: interview with {{ candidate_label }}",
        "Reminder: you interview {{ candidate_label }} for {{ job_title }} on "
        "{{ starts_at }}, for {{ duration_min }} minutes." + _JOIN_EN + _OPEN_EN,
        _PANEL,
    ),
    _t(
        PANEL_REMINDER,
        "ar",
        "تذكير: مقابلة {{ candidate_label }}",
        "تذكير: لديك مقابلة مع {{ candidate_label }} لوظيفة {{ job_title }} في "
        "{{ starts_at }}، لمدة {{ duration_min }} دقيقة." + _JOIN_AR + _OPEN_AR,
        _PANEL,
    ),
    _t(
        PANEL_REMINDER,
        "en",
        "Interview soon: {{ candidate_label }}",
        "{{ job_title }}, {{ starts_at }}, {{ duration_min }} minutes.",
        _INAPP_REMINDER_VARS,
        channel="in_app",
    ),
    _t(
        PANEL_REMINDER,
        "ar",
        "مقابلة قريبا: {{ candidate_label }}",
        "{{ job_title }}، {{ starts_at }}، {{ duration_min }} دقيقة.",
        _INAPP_REMINDER_VARS,
        channel="in_app",
    ),
)

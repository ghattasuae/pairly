# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""Interviewer messages of M3, English and Arabic.

* ``hire.interviewer.briefing`` version 2 (migration ``hire_0010``): the M2 briefing with
  the interview kit in place of the placeholder (``kit``: plain text, never a quote
  from the CV) and the one-click link to the interviewer's own scorecard
  (``scorecard_url``, single use, see ``interviewing.links``).
* ``hire.interviewer.scorecard_reminder`` version 1 (migration ``hire_0011``): your
  scorecard is due, with a fresh one-click link and a "remind me later" link.

Copy style: short plain sentences, never an em dash (the tests lint every template).
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate
from pairly_hire.scheduling.templates import BRIEFING

SCORECARD_REMINDER: Final = "hire.interviewer.scorecard_reminder"


def _v(kind: str, sample: Any = None, *, required: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": required}
    if sample is not None:
        data["sample"] = sample
    return data


_BRIEFING_V2_VARS: Final = {
    "candidate_label": _v("string", "Candidate 7KQ2-M9XD"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "starts_at": _v("datetime", "2026-10-13T06:00:00+00:00"),
    "duration_min": _v("integer", 45),
    "meeting_url": _v("url", "https://meet.pairly.example/abc-defg-hij", required=False),
    "interview_url": _v("url", "https://app.pairly.example/en/hire/jobs/sample", required=False),
    "summary": _v("text", "Stage: Interview. Screening: strong match on 3 of 4 criteria."),
    "panel": _v("string", "Omar, Sara"),
    "kit": _v("text", "1. System design\n   Ask: Walk me through a recent design.", required=False),
    "scorecard_url": _v("url", "https://app.pairly.example/en/scorecard#sample", required=False),
}
_REMINDER_VARS: Final = {
    "candidate_label": _v("string", "Candidate 7KQ2-M9XD"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "starts_at": _v("datetime", "2026-10-13T06:00:00+00:00"),
    "due_at": _v("datetime", "2026-10-14T06:45:00+00:00"),
    "overdue": _v("boolean", False),
    "scorecard_url": _v("url", "https://app.pairly.example/en/scorecard#sample", required=False),
    "later_url": _v("url", "https://app.pairly.example/en/scorecard#later", required=False),
}

_JOIN_EN = "{% if meeting_url %}\nJoin the call: {{ meeting_url }}{% endif %}"
_JOIN_AR = "{% if meeting_url %}\nرابط المكالمة: {{ meeting_url }}{% endif %}"
_OPEN_EN = "{% if interview_url %}\n\nOpen the candidate in Pairly: {{ interview_url }}{% endif %}"
_OPEN_AR = "{% if interview_url %}\n\nافتح ملف المرشح في Pairly: {{ interview_url }}{% endif %}"


def _t(
    key: str, locale: str, subject: str, body: str, variables: dict[str, Any]
) -> DefaultTemplate:
    return DefaultTemplate(
        key=key,
        channel="email",
        locale=locale,
        category="interviews",
        subject=subject,
        body=body,
        variables=variables,
    )


BRIEFING_V2: Final[tuple[DefaultTemplate, ...]] = (
    _t(
        BRIEFING,
        "en",
        "Interview: {{ candidate_label }}, {{ job_title }}",
        "You are on the interview panel for {{ job_title }}.\n\n"
        "When: {{ starts_at }}, {{ duration_min }} minutes." + _JOIN_EN + "\n"
        "Panel: {{ panel }}\n\n"
        "Candidate: {{ candidate_label }}\n{{ summary }}"
        "{% if scorecard_url %}\n\nYour scorecard (one click, works on your phone, about 3 "
        "minutes): {{ scorecard_url }}\nOthers' feedback stays hidden until you submit "
        "yours.{% endif %}"
        "{% if kit %}\n\nInterview kit:\n{{ kit }}{% endif %}" + _OPEN_EN,
        _BRIEFING_V2_VARS,
    ),
    _t(
        BRIEFING,
        "ar",
        "مقابلة: {{ candidate_label }}، {{ job_title }}",
        "أنت ضمن لجنة المقابلة لوظيفة {{ job_title }}.\n\n"
        "الموعد: {{ starts_at }}، لمدة {{ duration_min }} دقيقة." + _JOIN_AR + "\n"
        "اللجنة: {{ panel }}\n\n"
        "المرشح: {{ candidate_label }}\n{{ summary }}"
        "{% if scorecard_url %}\n\nبطاقة تقييمك (بنقرة واحدة، تعمل على هاتفك، نحو 3 دقائق): "
        "{{ scorecard_url }}\nتبقى تقييمات الآخرين مخفية حتى ترسل تقييمك.{% endif %}"
        "{% if kit %}\n\nدليل المقابلة:\n{{ kit }}{% endif %}" + _OPEN_AR,
        _BRIEFING_V2_VARS,
    ),
)

SCORECARD_REMINDERS: Final[tuple[DefaultTemplate, ...]] = (
    _t(
        SCORECARD_REMINDER,
        "en",
        "{% if overdue %}Overdue: {% endif %}your scorecard for {{ candidate_label }}",
        "Your scorecard for the interview with {{ candidate_label }} for {{ job_title }} "
        "({{ starts_at }}) is "
        "{% if overdue %}overdue{% else %}due by {{ due_at }}{% endif %}.\n\n"
        "It takes about 3 minutes, and others' feedback stays hidden until you submit."
        "{% if scorecard_url %}\n\nOpen your scorecard: {{ scorecard_url }}{% endif %}"
        "{% if later_url %}\n\nNot now? Remind me later: {{ later_url }}{% endif %}",
        _REMINDER_VARS,
    ),
    _t(
        SCORECARD_REMINDER,
        "ar",
        "{% if overdue %}متأخر: {% endif %}بطاقة تقييمك للمرشح {{ candidate_label }}",
        "بطاقة تقييمك لمقابلة {{ candidate_label }} لوظيفة {{ job_title }} "
        "({{ starts_at }}) "
        "{% if overdue %}متأخرة{% else %}مطلوبة قبل {{ due_at }}{% endif %}.\n\n"
        "تستغرق نحو 3 دقائق، وتبقى تقييمات الآخرين مخفية حتى ترسل تقييمك."
        "{% if scorecard_url %}\n\nافتح بطاقة تقييمك: {{ scorecard_url }}{% endif %}"
        "{% if later_url %}\n\nليس الآن؟ ذكرني لاحقا: {{ later_url }}{% endif %}",
        _REMINDER_VARS,
    ),
)

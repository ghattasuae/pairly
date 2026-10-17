# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""The Hire message library (brief 3.5.1): system templates, English and Arabic.

Candidate messages (email):

* ``hire.candidate.status_update``: where an application stands (non-transactional:
  the candidate can turn these off).
* ``hire.candidate.interview_invite``, ``.interview_reschedule``, ``.interview_reminder``.
* Rejections, all sensitive (``requires_approval``): ``hire.candidate.rejection`` (the
  core default, any stage), ``.rejection_screening`` (after the application review),
  ``.rejection_interview`` (after interviews, warmer), ``.rejection_final`` (final
  round, asks to keep the person in mind).
* ``hire.candidate.offer`` (the core default, sensitive).
* ``hire.candidate.withdrawal``: confirms the candidate withdrew.

The acknowledgement (``hire.application.received``) lives in ``pairly_hire.templates``.

Messages for the hiring team: ``hire.note.mention`` (in the app) and ``hire.sla.waiting``
(email and in the app, category ``sla``: an SLA breach is urgent).

Migration ``hire_0003`` stores ``SYSTEM_TEMPLATES`` as version 1 shared rows. The
rejection and offer defaults are the core rows of migration 0011 (``CORE_KEYS``); this
module only describes them.

Every candidate template that allows it declares ``personal_note`` (optional text): the
one place an AI draft writes (``pairly_hire.messaging.drafting``). Rejections also
declare ``reason`` (optional): a short reason, only from rubric evidence, always
approved by a person. Everything else comes from the candidate's record or from the
person writing the message, never from a model.

Copy style: short plain sentences, never an em dash (``tests`` lint every template).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from pairly_core.notifications.defaults import SENSITIVE, DefaultTemplate

STATUS_UPDATE: Final = "hire.candidate.status_update"
INTERVIEW_INVITE: Final = "hire.candidate.interview_invite"
INTERVIEW_RESCHEDULE: Final = "hire.candidate.interview_reschedule"
INTERVIEW_REMINDER: Final = "hire.candidate.interview_reminder"
REJECTION: Final = "hire.candidate.rejection"
REJECTION_SCREENING: Final = "hire.candidate.rejection_screening"
REJECTION_INTERVIEW: Final = "hire.candidate.rejection_interview"
REJECTION_FINAL: Final = "hire.candidate.rejection_final"
OFFER: Final = "hire.candidate.offer"
WITHDRAWAL: Final = "hire.candidate.withdrawal"
NOTE_MENTION: Final = "hire.note.mention"
SLA_WAITING: Final = "hire.sla.waiting"

CORE_KEYS: Final = frozenset({REJECTION, OFFER})

# Variables the record fills (never typed, never written by a model).
RECORD_VARIABLES: Final = frozenset(
    {"candidate_name", "job_title", "company", "reference", "portal_url", "preferences_url"}
)
# Free text a model may write (each one optional in its template).
AI_VARIABLES: Final = frozenset({"personal_note", "reason"})


def _v(kind: str, sample: Any = None, *, required: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "required": required}
    if sample is not None:
        data["sample"] = sample
    return data


_BASE: Final = {
    "candidate_name": _v("string", "Layla Haddad"),
    "job_title": _v("string", "Senior Backend Engineer"),
    "company": _v("string", "Acme Logistics"),
    "personal_note": _v("text", required=False),
}
_STATUS_VARS: Final = _BASE | {
    "stage_label": _v("string", "Screening"),
    "preferences_url": _v(
        "url", "https://app.pairly.example/en/candidate/preferences#sample", required=False
    ),
}
_INTERVIEW_VARS: Final = _BASE | {
    "interview_details": _v("text", "A 45 minute video call with the team on Tuesday at 10:00."),
    "schedule_url": _v("url", "https://app.pairly.example/en/schedule#sample", required=False),
}
_REMINDER_VARS: Final = _BASE | {
    "interview_details": _v("text", "A 45 minute video call with the team on Tuesday at 10:00."),
}
_REJECTION_VARS: Final = _BASE | {"reason": _v("text", required=False)}
_FINAL_VARS: Final = _REJECTION_VARS | {
    "preferences_url": _v(
        "url", "https://app.pairly.example/en/candidate/preferences#sample", required=False
    ),
}
_MENTION_VARS: Final = {
    "candidate_label": _v("string", "Candidate 7KQ2-M9XD"),
    "job_title": _v("string", "Senior Backend Engineer"),
}
_SLA_VARS: Final = {
    "job_title": _v("string", "Senior Backend Engineer"),
    "count": _v("integer", 3),
    "days": _v("integer", 7),
    "pipeline_url": _v("url", "https://app.pairly.example/en/hire/jobs/sample/pipeline"),
}

_NOTE_EN = "{% if personal_note %}\n\n{{ personal_note }}{% endif %}"
_REASON_EN = "{% if reason %}\n\n{{ reason }}{% endif %}"
_PREFS_EN = (
    "{% if preferences_url %}\n\nTo stop these updates, or to change what we may do with "
    "your details: {{ preferences_url }}{% endif %}"
)
_PREFS_AR = (
    "{% if preferences_url %}\n\nلإيقاف هذه التحديثات، أو لتغيير ما يمكننا فعله ببياناتك: "
    "{{ preferences_url }}{% endif %}"
)
_FINAL_PREFS_EN = (
    "{% if preferences_url %}\n\nIf you agree, we would like to keep your details and "
    "contact you about future roles. You can say yes or no here: {{ preferences_url }}"
    "{% endif %}"
)
_FINAL_PREFS_AR = (
    "{% if preferences_url %}\n\nإذا وافقت، نود الاحتفاظ ببياناتك والتواصل معك بشأن "
    "وظائف مستقبلية. يمكنك القبول أو الرفض من هنا: {{ preferences_url }}{% endif %}"
)


def _t(
    key: str,
    locale: str,
    category: str,
    subject: str,
    body: str,
    variables: dict[str, Any],
    *,
    channel: str = "email",
    sensitive: bool = False,
) -> DefaultTemplate:
    return DefaultTemplate(
        key=key,
        channel=channel,
        locale=locale,
        category=category,
        subject=subject,
        body=body,
        variables=variables,
        requires_approval=sensitive,
    )


SYSTEM_TEMPLATES: Final[tuple[DefaultTemplate, ...]] = (
    # ------------------------------------------------------------ status update
    _t(
        STATUS_UPDATE,
        "en",
        "updates",
        "An update on your application: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "Your application for {{ job_title }} at {{ company }} is at this step: "
        "{{ stage_label }}. A person on our team is working on it."
        + _NOTE_EN
        + "\n\nWe will write again when there is news.\n\n{{ company }}"
        + _PREFS_EN,
        _STATUS_VARS,
    ),
    _t(
        STATUS_UPDATE,
        "ar",
        "updates",
        "تحديث بشأن طلبك: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "طلبك لوظيفة {{ job_title }} لدى {{ company }} في هذه المرحلة: {{ stage_label }}. "
        "يعمل عليه شخص من فريقنا."
        + _NOTE_EN
        + "\n\nسنكتب لك مرة أخرى عندما تكون هناك أخبار.\n\n{{ company }}"
        + _PREFS_AR,
        _STATUS_VARS,
    ),
    # ------------------------------------------------------------ interviews
    _t(
        INTERVIEW_INVITE,
        "en",
        "interviews",
        "Interview invitation: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "Thank you for applying for {{ job_title }} at {{ company }}. We would like to "
        "meet you.\n\n{{ interview_details }}"
        "{% if schedule_url %}\n\nChoose a time that suits you: {{ schedule_url }}{% endif %}"
        + _NOTE_EN
        + "\n\nReply to this email if you need any adjustment for the interview.\n\n"
        "{{ company }}",
        _INTERVIEW_VARS,
    ),
    _t(
        INTERVIEW_INVITE,
        "ar",
        "interviews",
        "دعوة لمقابلة: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "شكرا لتقديمك على وظيفة {{ job_title }} لدى {{ company }}. نود أن نلتقي بك."
        "\n\n{{ interview_details }}"
        "{% if schedule_url %}\n\nاختر وقتا يناسبك: {{ schedule_url }}{% endif %}"
        + _NOTE_EN
        + "\n\nرد على هذه الرسالة إذا احتجت إلى أي تعديل للمقابلة.\n\n{{ company }}",
        _INTERVIEW_VARS,
    ),
    _t(
        INTERVIEW_RESCHEDULE,
        "en",
        "interviews",
        "New time for your interview: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "We need to move your interview for {{ job_title }} at {{ company }}. We are sorry "
        "for the change.\n\n{{ interview_details }}"
        "{% if schedule_url %}\n\nIf this time does not suit you, choose another one: "
        "{{ schedule_url }}{% endif %}" + _NOTE_EN + "\n\n{{ company }}",
        _INTERVIEW_VARS,
    ),
    _t(
        INTERVIEW_RESCHEDULE,
        "ar",
        "interviews",
        "موعد جديد لمقابلتك: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "نحتاج إلى تغيير موعد مقابلتك لوظيفة {{ job_title }} لدى {{ company }}. نعتذر عن "
        "هذا التغيير.\n\n{{ interview_details }}"
        "{% if schedule_url %}\n\nإذا لم يناسبك هذا الموعد، اختر موعدا آخر: "
        "{{ schedule_url }}{% endif %}" + _NOTE_EN + "\n\n{{ company }}",
        _INTERVIEW_VARS,
    ),
    _t(
        INTERVIEW_REMINDER,
        "en",
        "interviews",
        "Reminder: your interview for {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "This is a reminder of your interview for {{ job_title }} at {{ company }}.\n\n"
        "{{ interview_details }}" + _NOTE_EN + "\n\nWe look forward to it.\n\n{{ company }}",
        _REMINDER_VARS,
    ),
    _t(
        INTERVIEW_REMINDER,
        "ar",
        "interviews",
        "تذكير: مقابلتك لوظيفة {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "هذا تذكير بمقابلتك لوظيفة {{ job_title }} لدى {{ company }}.\n\n"
        "{{ interview_details }}" + _NOTE_EN + "\n\nنتطلع إلى لقائك.\n\n{{ company }}",
        _REMINDER_VARS,
    ),
    # ------------------------------------------------------------ rejections
    _t(
        REJECTION_SCREENING,
        "en",
        "candidate_comms",
        "Your application for {{ job_title }}",
        "Dear {{ candidate_name }},\n\n"
        "Thank you for applying for {{ job_title }} at {{ company }}. A person on our team "
        "read your application carefully. We have decided not to move forward with it."
        + _REASON_EN
        + _NOTE_EN
        + "\n\nWe wish you well in your search.\n\n{{ company }}",
        _REJECTION_VARS,
        sensitive=True,
    ),
    _t(
        REJECTION_SCREENING,
        "ar",
        "candidate_comms",
        "طلبك لوظيفة {{ job_title }}",
        "عزيزنا {{ candidate_name }}،\n\n"
        "شكرا لتقديمك على وظيفة {{ job_title }} لدى {{ company }}. قرأ شخص من فريقنا طلبك "
        "بعناية. قررنا عدم المضي فيه." + _REASON_EN + _NOTE_EN + "\n\nنتمنى لك التوفيق في "
        "بحثك.\n\n{{ company }}",
        _REJECTION_VARS,
        sensitive=True,
    ),
    _t(
        REJECTION_INTERVIEW,
        "en",
        "candidate_comms",
        "Your interview for {{ job_title }}",
        "Dear {{ candidate_name }},\n\n"
        "Thank you for the time you spent with our team for {{ job_title }} at "
        "{{ company }}. It was good to meet you. After careful thought, we have decided "
        "not to offer you the role." + _REASON_EN + _NOTE_EN + "\n\nWe are grateful for "
        "your interest and wish you every success.\n\n{{ company }}",
        _REJECTION_VARS,
        sensitive=True,
    ),
    _t(
        REJECTION_INTERVIEW,
        "ar",
        "candidate_comms",
        "مقابلتك لوظيفة {{ job_title }}",
        "عزيزنا {{ candidate_name }}،\n\n"
        "شكرا على الوقت الذي قضيته مع فريقنا من أجل وظيفة {{ job_title }} لدى "
        "{{ company }}. سعدنا بلقائك. بعد تفكير متأن، قررنا عدم تقديم عرض لك لهذه "
        "الوظيفة." + _REASON_EN + _NOTE_EN + "\n\nنقدر اهتمامك ونتمنى لك كل النجاح."
        "\n\n{{ company }}",
        _REJECTION_VARS,
        sensitive=True,
    ),
    _t(
        REJECTION_FINAL,
        "en",
        "candidate_comms",
        "Your application for {{ job_title }}",
        "Dear {{ candidate_name }},\n\n"
        "Thank you for staying with us through every step for {{ job_title }} at "
        "{{ company }}. This was a close decision, and we have chosen another candidate."
        + _REASON_EN
        + _NOTE_EN
        + _FINAL_PREFS_EN
        + "\n\nThank you again for your time.\n\n{{ company }}",
        _FINAL_VARS,
        sensitive=True,
    ),
    _t(
        REJECTION_FINAL,
        "ar",
        "candidate_comms",
        "طلبك لوظيفة {{ job_title }}",
        "عزيزنا {{ candidate_name }}،\n\n"
        "شكرا لبقائك معنا في كل خطوة لوظيفة {{ job_title }} لدى {{ company }}. كان القرار "
        "صعبا، واخترنا مرشحا آخر."
        + _REASON_EN
        + _NOTE_EN
        + _FINAL_PREFS_AR
        + "\n\nشكرا مرة أخرى على وقتك.\n\n{{ company }}",
        _FINAL_VARS,
        sensitive=True,
    ),
    # ------------------------------------------------------------ withdrawal
    _t(
        WITHDRAWAL,
        "en",
        "candidate_comms",
        "You withdrew your application: {{ job_title }}",
        "Hello {{ candidate_name }},\n\n"
        "We confirm that you withdrew your application for {{ job_title }} at "
        "{{ company }}. We will not contact you about it again." + _NOTE_EN + "\n\n"
        "Thank you for your interest. You are welcome to apply again in the future.\n\n"
        "{{ company }}",
        _BASE,
    ),
    _t(
        WITHDRAWAL,
        "ar",
        "candidate_comms",
        "سحبت طلبك: {{ job_title }}",
        "مرحبا {{ candidate_name }}،\n\n"
        "نؤكد أنك سحبت طلبك لوظيفة {{ job_title }} لدى {{ company }}. لن نتواصل معك بشأنه "
        "مرة أخرى." + _NOTE_EN + "\n\nشكرا لاهتمامك. يسعدنا أن تتقدم مرة أخرى في المستقبل."
        "\n\n{{ company }}",
        _BASE,
    ),
    # ------------------------------------------------------------ hiring team
    _t(
        NOTE_MENTION,
        "en",
        "updates",
        "You were mentioned: {{ candidate_label }}",
        "A colleague mentioned you in a note on {{ candidate_label }} ({{ job_title }}).",
        _MENTION_VARS,
        channel="in_app",
    ),
    _t(
        NOTE_MENTION,
        "ar",
        "updates",
        "تمت الإشارة إليك: {{ candidate_label }}",
        "أشار إليك زميل في ملاحظة على {{ candidate_label }} ({{ job_title }}).",
        _MENTION_VARS,
        channel="in_app",
    ),
    _t(
        SLA_WAITING,
        "en",
        "sla",
        "{{ count }} candidates are waiting for an update: {{ job_title }}",
        "{{ count }} candidates for {{ job_title }} have had no update for {{ days }} days "
        "or more. A status update is drafted for each of them. Read and send it from the "
        "pipeline: {{ pipeline_url }}",
        _SLA_VARS,
    ),
    _t(
        SLA_WAITING,
        "ar",
        "sla",
        "{{ count }} مرشحين ينتظرون تحديثا: {{ job_title }}",
        "لم يصل {{ count }} مرشحين لوظيفة {{ job_title }} أي تحديث منذ {{ days }} أيام أو "
        "أكثر. جهزنا مسودة تحديث لكل منهم. اقرأها وأرسلها من مسار التوظيف: {{ pipeline_url }}",
        _SLA_VARS,
    ),
    _t(
        SLA_WAITING,
        "en",
        "sla",
        "{{ count }} candidates are waiting: {{ job_title }}",
        "No update for {{ days }} days or more. A status update is drafted for each.",
        {k: _SLA_VARS[k] for k in ("job_title", "count", "days")},
        channel="in_app",
    ),
    _t(
        SLA_WAITING,
        "ar",
        "sla",
        "{{ count }} مرشحين ينتظرون: {{ job_title }}",
        "لا تحديث منذ {{ days }} أيام أو أكثر. جهزنا مسودة تحديث لكل منهم.",
        {k: _SLA_VARS[k] for k in ("job_title", "count", "days")},
        channel="in_app",
    ),
)


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """How the compose panel offers a candidate template."""

    key: str
    label: str
    label_ar: str
    sensitive: bool
    # Transactional: about the candidate's own application; always sent. Otherwise the
    # candidate can turn it off (``hire_candidate_prefs.updates_email``).
    transactional: bool
    # Rejection templates: a move to the rejected stage drafts one of these.
    rejection: bool = False
    # Stages where the panel suggests it first.
    suggested_for: tuple[str, ...] = field(default=())


LIBRARY: Final[tuple[LibraryEntry, ...]] = (
    LibraryEntry(
        STATUS_UPDATE,
        "Status update",
        "تحديث الحالة",
        sensitive=False,
        transactional=False,
        suggested_for=("applied", "screening", "shortlist"),
    ),
    LibraryEntry(
        INTERVIEW_INVITE,
        "Interview invitation",
        "دعوة لمقابلة",
        sensitive=False,
        transactional=True,
        suggested_for=("shortlist", "interview"),
    ),
    LibraryEntry(
        INTERVIEW_RESCHEDULE,
        "New interview time",
        "موعد مقابلة جديد",
        sensitive=False,
        transactional=True,
        suggested_for=("interview",),
    ),
    LibraryEntry(
        INTERVIEW_REMINDER,
        "Interview reminder",
        "تذكير بالمقابلة",
        sensitive=False,
        transactional=True,
        suggested_for=("interview",),
    ),
    LibraryEntry(
        REJECTION_SCREENING,
        "Rejection after review",
        "اعتذار بعد المراجعة",
        sensitive=True,
        transactional=True,
        rejection=True,
        suggested_for=("applied", "screening"),
    ),
    LibraryEntry(
        REJECTION_INTERVIEW,
        "Rejection after interview",
        "اعتذار بعد المقابلة",
        sensitive=True,
        transactional=True,
        rejection=True,
        suggested_for=("interview",),
    ),
    LibraryEntry(
        REJECTION_FINAL,
        "Rejection after the final round",
        "اعتذار بعد المرحلة الأخيرة",
        sensitive=True,
        transactional=True,
        rejection=True,
        suggested_for=("offer",),
    ),
    LibraryEntry(
        REJECTION,
        "Rejection, short",
        "اعتذار مختصر",
        sensitive=True,
        transactional=True,
        rejection=True,
        suggested_for=("shortlist",),
    ),
    LibraryEntry(
        OFFER,
        "Offer",
        "عرض عمل",
        sensitive=True,
        transactional=True,
        suggested_for=("offer",),
    ),
    LibraryEntry(
        WITHDRAWAL,
        "Withdrawal confirmation",
        "تأكيد سحب الطلب",
        sensitive=False,
        transactional=True,
        suggested_for=("withdrawn",),
    ),
)
BY_KEY: Final = {entry.key: entry for entry in LIBRARY}


def entry(key: str) -> LibraryEntry | None:
    return BY_KEY.get(key)


def rejection_template_for(stage_key: str) -> str:
    """The rejection a move from ``stage_key`` to rejected drafts (tone by stage)."""
    if stage_key in {"interview"}:
        return REJECTION_INTERVIEW
    if stage_key in {"offer"}:
        return REJECTION_FINAL
    if stage_key in {"shortlist"}:
        return REJECTION
    return REJECTION_SCREENING


def definitions(key: str) -> list[DefaultTemplate]:
    """Every shipped definition of ``key`` (this library or the core defaults)."""
    pool = (*SYSTEM_TEMPLATES, *SENSITIVE)
    return [t for t in pool if t.key == key]

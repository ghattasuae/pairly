# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""The structured reference form, version 1, English and Arabic (brief 3.8.2).

Short and the same for every referee, so answers compare: how they know the candidate,
where and when they worked together, the candidate's title then, strengths, what to
develop, reliability (1 to 5), whether they would work with them again, and anything
else. A draft may be partial (progress saving); a submission must answer every
required question (``check_answers``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from pairly_core.errors import BadRequestError

FORM_VERSION: Final = "reference/v1"
MAX_TEXT: Final = 1000
MAX_LINE: Final = 120
_MONTH_RE: Final = re.compile(r"^(19[5-9][0-9]|20[0-9]{2})-(0[1-9]|1[0-2])$")
REHIRE: Final = ("yes", "no", "unsure")
KNOWN_AS: Final = ("manager", "peer", "direct_report", "client", "other")


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    kind: str  # choice, line, month, text, rating
    required: bool
    label: str
    label_ar: str
    options: tuple[str, ...] = ()


QUESTIONS: Final[tuple[Question, ...]] = (
    Question(
        "known_as",
        "choice",
        True,
        "How do you know the candidate?",
        "ما علاقتك بالمرشح؟",
        KNOWN_AS,
    ),
    Question("company", "line", True, "Where did you work together?", "أين عملتما معا؟"),
    Question(
        "candidate_title",
        "line",
        True,
        "What was the candidate's job title then?",
        "ما كان المسمى الوظيفي للمرشح حينها؟",
    ),
    Question("worked_from", "month", True, "Worked together from (month)", "عملتما معا من (الشهر)"),
    Question(
        "worked_to",
        "month",
        False,
        "Until (month; leave empty if you still work together)",
        "حتى (الشهر؛ اتركه فارغا إن كنتما ما زلتما تعملان معا)",
    ),
    Question(
        "strengths",
        "text",
        True,
        "What are the candidate's main strengths? An example helps.",
        "ما أبرز نقاط قوة المرشح؟ يفيد ذكر مثال.",
    ),
    Question(
        "development",
        "text",
        True,
        "What could the candidate develop further?",
        "ما الذي يمكن للمرشح تطويره أكثر؟",
    ),
    Question(
        "reliability",
        "rating",
        True,
        "How reliable were they in delivering what they committed to? (1 low, 5 high)",
        "ما مدى التزامه بإنجاز ما تعهد به؟ (1 منخفض، 5 مرتفع)",
    ),
    Question(
        "would_rehire",
        "choice",
        True,
        "Would you work with them again?",
        "هل تعمل معه مرة أخرى؟",
        REHIRE,
    ),
    Question(
        "anything_else",
        "text",
        False,
        "Anything else we should know? (optional)",
        "هل هناك ما ينبغي أن نعرفه أيضا؟ (اختياري)",
    ),
)
BY_ID: Final = {q.id: q for q in QUESTIONS}


def _clean(question: Question, value: Any) -> Any:
    """One answer, normalised, or ``BadRequestError``. Empty means not answered."""
    if value is None:
        return None
    if question.kind == "rating":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise BadRequestError(f"{question.id}: a rating from 1 to 5")
        return value
    if not isinstance(value, str):
        raise BadRequestError(f"{question.id}: text expected")
    text = value.strip()
    if not text:
        return None
    if question.kind == "choice" and text not in question.options:
        raise BadRequestError(f"{question.id}: choose one of the options")
    if question.kind == "month" and not _MONTH_RE.fullmatch(text):
        raise BadRequestError(f"{question.id}: a month as YYYY-MM")
    limit = MAX_LINE if question.kind in {"line", "month", "choice"} else MAX_TEXT
    if len(text) > limit:
        raise BadRequestError(f"{question.id}: at most {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise BadRequestError(f"{question.id}: remove control characters")
    return text


def clean_answers(raw: dict[str, Any]) -> dict[str, Any]:
    """The answers of a draft: known questions only, each valid; unanswered dropped."""
    unknown = set(raw) - set(BY_ID)
    if unknown:
        raise BadRequestError("the form has no such question")
    out: dict[str, Any] = {}
    for question in QUESTIONS:
        value = _clean(question, raw.get(question.id))
        if value is not None:
            out[question.id] = value
    return out


def missing(answers: dict[str, Any]) -> list[str]:
    return [q.id for q in QUESTIONS if q.required and answers.get(q.id) in (None, "")]


def check_answers(raw: dict[str, Any]) -> dict[str, Any]:
    """The answers of a submission: every required question answered, dates in order."""
    answers = clean_answers(raw)
    absent = missing(answers)
    if absent:
        raise BadRequestError(f"answer every required question ({', '.join(absent)})")
    start, end = answers.get("worked_from"), answers.get("worked_to")
    if isinstance(start, str) and isinstance(end, str) and end < start:
        raise BadRequestError("worked_to: the end comes after the start")
    return answers


def questions_out(locale: str) -> list[dict[str, Any]]:
    ar = locale == "ar"
    return [
        {
            "id": q.id,
            "kind": q.kind,
            "required": q.required,
            "label": q.label_ar if ar else q.label,
            "options": list(q.options),
        }
        for q in QUESTIONS
    ]

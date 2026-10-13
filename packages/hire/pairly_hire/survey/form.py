# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""The candidate experience survey, version 1: 5 short questions, English and Arabic.

Three ratings from 1 (poor) to 5 (excellent), whether they would recommend applying,
and an optional comment. Every answer is optional except the overall rating, so the
survey takes under a minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pairly_core.errors import BadRequestError

FORM_VERSION: Final = "survey/v1"
MAX_COMMENT: Final = 1000
RATINGS: Final = ("overall", "communication", "respect")
RECOMMEND: Final = ("yes", "no", "unsure")


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    kind: str  # rating, choice, text
    required: bool
    label: str
    label_ar: str
    options: tuple[str, ...] = ()


QUESTIONS: Final[tuple[Question, ...]] = (
    Question(
        "overall",
        "rating",
        True,
        "Overall, how was your experience applying with us?",
        "بشكل عام، كيف كانت تجربتك في التقدم لدينا؟",
    ),
    Question(
        "communication",
        "rating",
        False,
        "How clear and timely were our updates?",
        "ما مدى وضوح تحديثاتنا وسرعتها؟",
    ),
    Question(
        "respect",
        "rating",
        False,
        "Did you feel treated fairly and with respect?",
        "هل شعرت بأنك عوملت بإنصاف واحترام؟",
    ),
    Question(
        "recommend",
        "choice",
        False,
        "Would you recommend applying here to a friend?",
        "هل تنصح صديقا بالتقدم لدينا؟",
        RECOMMEND,
    ),
    Question(
        "comment",
        "text",
        False,
        "Anything we could do better? (optional)",
        "هل من شيء يمكننا تحسينه؟ (اختياري)",
    ),
)


def check_answers(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The structured answers and the comment (kept apart: the comment is encrypted)."""
    known = {q.id for q in QUESTIONS}
    if set(raw) - known:
        raise BadRequestError("the survey has no such question")
    out: dict[str, Any] = {}
    for key in RATINGS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise BadRequestError(f"{key}: a rating from 1 to 5")
        out[key] = value
    if "overall" not in out:
        raise BadRequestError("overall: a rating from 1 to 5")
    recommend = raw.get("recommend")
    if recommend is not None:
        if recommend not in RECOMMEND:
            raise BadRequestError("recommend: yes, no or unsure")
        out["recommend"] = recommend
    comment = raw.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise BadRequestError("comment: text")
    text = " ".join((comment or "").split())
    if len(text) > MAX_COMMENT:
        raise BadRequestError(f"comment: at most {MAX_COMMENT} characters")
    return out, (text or None)


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

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Knockout handling (brief 3.3.8): a different answer goes to a person, never a rejection.

``check_knockouts(rubric, answers)`` compares each knockout answer with the answer the
locked rubric needs. A mismatch, or a question left unanswered, gives a result with a
suggested action for the reviewer. The suggestions are things a person does
(``SUGGESTED_ACTIONS``); there is no "reject" among them, and nothing in screening
changes an application's status because of a knockout.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pairly_hire.rubric import Rubric

SuggestedAction = Literal["confirm_with_candidate", "ask_candidate"]
SUGGESTED_ACTIONS: Final[frozenset[str]] = frozenset({"confirm_with_candidate", "ask_candidate"})


@dataclass(frozen=True, slots=True)
class KnockoutCheck:
    id: str
    question: str
    expected_answer: str
    answer: str | None
    matches: bool
    reason: str
    suggested_action: SuggestedAction | None
    suggestion: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "expected_answer": self.expected_answer,
            "answer": self.answer,
            "matches": self.matches,
            "reason": self.reason,
            "route": "pass" if self.matches else "human_review",
            "suggested_action": self.suggested_action,
            "suggestion": self.suggestion,
        }


def check_knockouts(rubric: Rubric, answers: Mapping[str, str]) -> list[KnockoutCheck]:
    out: list[KnockoutCheck] = []
    for question in rubric.knockout_questions:
        raw = answers.get(question.id)
        answer = raw.strip().casefold() if isinstance(raw, str) and raw.strip() else None
        matches = answer == question.expected_answer
        action: SuggestedAction | None = None
        suggestion: str | None = None
        if answer is None:
            action = "ask_candidate"
            suggestion = "The question was not answered. Ask the candidate before deciding."
        elif not matches:
            action = "confirm_with_candidate"
            suggestion = (
                f"The answer is {answer!r}; the job needs {question.expected_answer!r} "
                f"({question.reason.rstrip('.')}). Confirm with the candidate before deciding."
            )
        out.append(
            KnockoutCheck(
                id=question.id,
                question=question.question,
                expected_answer=question.expected_answer,
                answer=answer,
                matches=matches,
                reason=question.reason,
                suggested_action=action,
                suggestion=suggestion,
            )
        )
    return out


def knockout_reasons(checks: list[KnockoutCheck]) -> list[str]:
    """Review reasons, one per question that did not match."""
    return [f"knockout:{c.id}" for c in checks if not c.matches]

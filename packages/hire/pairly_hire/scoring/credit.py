# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""No evidence, no credit: turn the model's answer into credited criterion scores.

``credit(output, rubric, doc, model_input)`` checks every criterion answer:

* every quote must be found in the text the model read (``model_input``: the blinded,
  sanitised CV) and map back to one contiguous range of the original CV whose text
  matches it (``pairly_core.extraction.evidence.EvidenceLocator``). A quote of hidden
  text or of an instruction the guardrails removed is not in that text or does not map
  back, so it is rejected. A quote that copies a blind placeholder ("BSc Accounting,
  [university], 2019") is credited with its visible parts only, each located on its
  own; the hidden text never becomes evidence;
* a score above 0 with no accepted quote is credited 0 (``no_evidence``);
* the "why not higher" text is checked as plain text (no markup, links or protected
  attributes, ``pairly_core.guardrails.validation.HIRING_POLICY``); anything else is
  dropped and noted;
* a criterion the model did not answer counts 0 and needs a person (``not_scored``).

The overall score is then computed from the credited scores and the rubric weights
(``aggregate.overall_score``). Nothing the model writes can set it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from pairly_core.documents.model import NormalizedDocument
from pairly_core.extraction.evidence import EvidenceLocator, SanitizedInput
from pairly_core.guardrails.validation import HIRING_POLICY, check_banned_content
from pairly_hire.rubric import Rubric
from pairly_hire.scoring.aggregate import breakdown
from pairly_hire.scoring.model import CriterionScore, CvScoreOutput

LOW_CONFIDENCE: Final = 0.5
_NOT_TEXT = re.compile(r"[<>`]|https?://|www\.|\]\(|[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class Evidence:
    """An accepted quote: the exact original text and where it is."""

    quote: str
    start: int
    end: int
    page: int | None
    bboxes: tuple[dict[str, float], ...] = ()

    def location(self) -> dict[str, Any]:
        """Public part (no CV text): offsets, page and boxes."""
        return {
            "start": self.start,
            "end": self.end,
            "page": self.page,
            "bboxes": list(self.bboxes),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"quote": self.quote, **self.location()}


@dataclass(frozen=True, slots=True)
class Rejected:
    quote: str
    reason: str


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion_id: str
    name: str
    weight: int
    must_have: bool
    model_score: int | None
    score: int
    confidence: float
    evidence: tuple[Evidence, ...]
    rejected: tuple[Rejected, ...]
    why_not_higher: str
    flags: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        """Without CV text (decision record ``output`` and ``evidence``)."""
        return {
            "criterion_id": self.criterion_id,
            "weight": self.weight,
            "must_have": self.must_have,
            "model_score": self.model_score,
            "score": self.score,
            "confidence": self.confidence,
            "evidence": [e.location() for e in self.evidence],
            "rejected": [{"reason": r.reason} for r in self.rejected],
            "flags": list(self.flags),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.public(),
            "name": self.name,
            "evidence": [e.as_dict() for e in self.evidence],
            "rejected": [{"quote": r.quote, "reason": r.reason} for r in self.rejected],
            "why_not_higher": self.why_not_higher,
        }


@dataclass(frozen=True, slots=True)
class ScoringResult:
    criteria: tuple[CriterionResult, ...]
    overall: float
    shares: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def scores(self) -> dict[str, int]:
        return {c.criterion_id: c.score for c in self.criteria}

    @property
    def evidence_count(self) -> int:
        return sum(len(c.evidence) for c in self.criteria)


def _text_only(value: str) -> tuple[str, str | None]:
    text = " ".join(value.split())
    if not text:
        return "", None
    if _NOT_TEXT.search(text):
        return "", "not_plain_text"
    issues = check_banned_content(text, HIRING_POLICY)
    if issues:
        return "", issues[0].code
    return text, None


def credit(
    output: CvScoreOutput,
    rubric: Rubric,
    doc: NormalizedDocument,
    model_input: SanitizedInput,
) -> ScoringResult:
    locator = EvidenceLocator(doc, model_input)
    answers: dict[str, CriterionScore] = {}
    reasons: list[str] = []
    known = {c.id for c in rubric.criteria}
    for answer in output.criteria:
        if answer.criterion_id not in known:
            reasons.append("score_unknown_criterion")
            continue
        if answer.criterion_id in answers:
            reasons.append(f"score_duplicate:{answer.criterion_id}")
            continue
        answers[answer.criterion_id] = answer
    must = {c.id for c in rubric.must_haves}
    results: list[CriterionResult] = []
    for criterion in rubric.criteria:
        found = answers.get(criterion.id)
        if found is None:
            reasons.append(f"score_missing:{criterion.id}")
            results.append(
                CriterionResult(
                    criterion.id,
                    criterion.name,
                    criterion.weight,
                    criterion.id in must,
                    None,
                    0,
                    0.0,
                    (),
                    (),
                    "",
                    ("not_scored",),
                )
            )
            continue
        answer = found
        accepted: list[Evidence] = []
        rejected: list[Rejected] = []
        seen: set[tuple[int, int]] = set()
        for quote in answer.evidence:
            parts = locator.locate_visible(quote)
            if not parts:
                rejected.append(Rejected(quote, "not_found"))
                continue
            for located in parts:
                if (located.start, located.end) in seen:
                    continue
                seen.add((located.start, located.end))
                first = located.spans[0]
                accepted.append(
                    Evidence(
                        quote=" ".join(s.quote for s in located.spans),
                        start=located.start,
                        end=located.end,
                        page=first.page,
                        bboxes=tuple(
                            b.model_dump(mode="json") for s in located.spans for b in s.bboxes
                        ),
                    )
                )
        flags: list[str] = []
        score = answer.score
        confidence = answer.confidence
        if score > 0 and not accepted:
            flags.append("no_evidence")
            reasons.append(f"score_no_evidence:{criterion.id}")
            score, confidence = 0, 0.0
        if rejected:
            flags.append("evidence_rejected")
        why, problem = _text_only(answer.why_not_higher)
        if problem is not None:
            flags.append(f"why_not_higher_dropped:{problem}")
        if score > 0 and confidence < LOW_CONFIDENCE:
            flags.append("low_confidence")
            reasons.append(f"score_low_confidence:{criterion.id}")
        results.append(
            CriterionResult(
                criterion_id=criterion.id,
                name=criterion.name,
                weight=criterion.weight,
                must_have=criterion.id in must,
                model_score=answer.score,
                score=score,
                confidence=confidence,
                evidence=tuple(accepted),
                rejected=tuple(rejected),
                why_not_higher=why,
                flags=tuple(flags),
            )
        )
    overall, shares = breakdown(rubric, {r.criterion_id: r.score for r in results})
    return ScoringResult(
        criteria=tuple(results),
        overall=overall,
        shares=tuple(s.as_dict() for s in shares),
        reasons=tuple(dict.fromkeys(reasons)),
    )

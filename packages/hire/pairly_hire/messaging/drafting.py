# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""AI-personalised message drafts (brief 3.5.2), checked before a person sees them.

The model (prompt ``hire_message_draft/v1`` through the model router) writes only:

* ``personal_note``: a short paragraph the template places in a fixed spot;
* ``reason``: for a rejection, one sentence from the screening's rubric evidence
  (``pairly_hire.pipeline.hooks.screening_summary``). Without evidence there is no
  reason: the check refuses one.

What the model sees (``draft_inputs``): the template, its purpose, the job title, the
language and, for a rejection, the evidence. Never the candidate's name, contact
details or anything else from the record: the template fills those in, so the model
has no personal data and cannot misuse it. This also makes recorded responses work for
any candidate of a demo job.

``check_draft`` is deterministic and fails closed. A draft is refused when it:

* is too long, holds an em dash, a link, an email address or a phone number;
* holds a number that is not in the inputs, or (English) a capitalised word that is
  not at a sentence start and not in the inputs (a name, a place, a company), or
  (Arabic) a Latin word that is not in the inputs;
* mentions a protected characteristic (age, gender, nationality, religion, health,
  family, appearance);
* gives a reason without evidence.

A refused draft changes nothing; the person writes the text themselves. The router
logs every call (prompt version and hash, inputs, output, cost).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_core.notifications.templating import has_em_dash
from pairly_hire.messaging import library
from pairly_hire.pipeline.hooks import ScreeningSummary

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

TASK: Final = TaskSpec(name="hire_message_draft", prompt_version="v1")
WORKFLOW: Final = "hire"
MAX_NOTE: Final = 400
MAX_REASON: Final = 250
MAX_EVIDENCE: Final = 8

# Templates an AI draft may personalise (they declare ``personal_note``).
AI_TEMPLATES: Final = (
    library.STATUS_UPDATE,
    library.INTERVIEW_INVITE,
    library.INTERVIEW_RESCHEDULE,
    library.INTERVIEW_REMINDER,
    library.REJECTION_SCREENING,
    library.REJECTION_INTERVIEW,
    library.REJECTION_FINAL,
    library.WITHDRAWAL,
)
PURPOSES: Final = {
    library.STATUS_UPDATE: "Tell the candidate their application is still being worked on.",
    library.INTERVIEW_INVITE: "Invite the candidate to an interview.",
    library.INTERVIEW_RESCHEDULE: "Tell the candidate their interview moves to a new time.",
    library.INTERVIEW_REMINDER: "Remind the candidate of their interview.",
    library.REJECTION_SCREENING: "Tell the candidate we will not move forward after review.",
    library.REJECTION_INTERVIEW: "Tell the candidate we will not offer the role after interviews.",
    library.REJECTION_FINAL: "Tell the candidate we chose someone else after the final round.",
    library.WITHDRAWAL: "Confirm the candidate withdrew their application.",
}

_Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE)]
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_REASON)]


class MessageDraft(BaseModel):
    """What the model returns. Both fields are shown to the person, who may edit them."""

    model_config = ConfigDict(extra="forbid")

    personal_note: _Note
    reason: _Reason = ""


class DraftRefusedError(ValueError):
    """The draft failed the checks (``problems``); nothing was saved."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def evidence_inputs(summary: ScreeningSummary | None) -> list[dict[str, Any]]:
    if summary is None:
        return []
    return [
        {"criterion": c.criterion, "result": c.result, "quotes": list(c.quotes[:3])}
        for c in summary.criteria[:MAX_EVIDENCE]
    ]


def draft_inputs(
    template_key: str,
    job_title: str,
    locale: str,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry = library.entry(template_key)
    return {
        "template": template_key,
        "purpose": PURPOSES.get(template_key, entry.label if entry else template_key),
        "job_title": job_title.strip(),
        "language": locale if locale in {"en", "ar"} else "en",
        "evidence": list(evidence or []) if entry is not None and entry.rejection else [],
    }


# ------------------------------------------------------------------ checks

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_NUMBER_RE = re.compile(r"\d+")
_CAPITAL_RE = re.compile(r"\b[A-Z][A-Za-z'-]*\b")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE_START_RE = re.compile(r"(^|[.!?:;]\s+|\n\s*)$")
_ARABIC_DIGITS: Final = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
# Common capitalised words that are not facts (sentence starts are allowed anyway).
_ALWAYS_OK: Final = frozenset({"i", "ai"})
_PROTECTED_EN: Final = (
    "age", "aged", "young", "old", "gender", "male", "female", "man", "woman", "nationality",
    "national", "citizen", "religion", "religious", "muslim", "christian", "pregnan",
    "married", "marital", "children", "family", "health", "disab", "illness", "race",
    "ethnic", "appearance", "photo", "accent",
)  # fmt: skip
_PROTECTED_AR: Final = (
    "العمر", "عمرك", "الجنس", "جنسيت", "الجنسية", "الدين", "ديان", "حامل", "الحمل",
    "متزوج", "الزواج", "أطفال", "الأسرة", "عائلت", "الصحة", "صحت", "إعاقة", "المظهر",
)  # fmt: skip


def _facts_text(inputs: dict[str, Any]) -> str:
    parts = [str(inputs.get("job_title", "")), str(inputs.get("purpose", ""))]
    for item in inputs.get("evidence") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("criterion", "")))
            parts.extend(str(q) for q in item.get("quotes") or [])
    return " ".join(parts).casefold()


def _protected(text: str, locale: str) -> str | None:
    words = re.findall(r"[a-z]+", text.casefold())
    for stem in _PROTECTED_EN:
        if any(w == stem or (len(stem) > 5 and w.startswith(stem)) for w in words):
            return stem
    if locale == "ar":
        for stem in _PROTECTED_AR:
            if re.search(rf"(^|\s|ال){re.escape(stem)}", text):
                return stem
    return None


def _check_text(field: str, text: str, facts: str, locale: str) -> list[str]:
    problems: list[str] = []
    if has_em_dash(text):
        problems.append(f"{field}: an em dash")
    if _URL_RE.search(text):
        problems.append(f"{field}: a link")
    if _EMAIL_RE.search(text):
        problems.append(f"{field}: an email address")
    if _PHONE_RE.search(text.translate(_ARABIC_DIGITS)):
        problems.append(f"{field}: a phone number")
    for number in _NUMBER_RE.findall(text.translate(_ARABIC_DIGITS)):
        if number not in facts:
            problems.append(f"{field}: the number {number} is not in the record")
    if locale == "ar":
        for word in _LATIN_RE.findall(text):
            if word.casefold() not in facts and word.casefold() not in _ALWAYS_OK:
                problems.append(f"{field}: {word!r} is not in the record")
    else:
        for match in _CAPITAL_RE.finditer(text):
            word = match.group(0)
            before = text[: match.start()]
            if _SENTENCE_START_RE.search(before):
                continue
            if word.casefold() in _ALWAYS_OK or word.casefold() in facts:
                continue
            problems.append(f"{field}: {word!r} is not in the record")
    found = _protected(text, locale)
    if found is not None:
        problems.append(f"{field}: mentions a protected characteristic ({found})")
    return problems


def check_draft(draft: MessageDraft, inputs: dict[str, Any]) -> list[str]:
    """Every reason to refuse the draft (empty: it may be shown). See the module doc."""
    facts = _facts_text(inputs)
    locale = str(inputs.get("language", "en"))
    problems: list[str] = []
    if not draft.personal_note:
        problems.append("personal_note: empty")
    problems += _check_text("personal_note", draft.personal_note, facts, locale)
    if draft.reason:
        if not inputs.get("evidence"):
            problems.append("reason: given without rubric evidence")
        problems += _check_text("reason", draft.reason, facts, locale)
    return sorted(set(problems))


# ------------------------------------------------------------------ drafting


@dataclass(frozen=True, slots=True)
class AiDraft:
    values: dict[str, str]
    call_id: uuid.UUID
    prompt_version: str
    model: str
    from_recording: bool
    reason_source: str | None


async def ai_draft(
    router: ModelRouter,
    org_id: uuid.UUID,
    *,
    template_key: str,
    job_title: str,
    locale: str,
    summary: ScreeningSummary | None,
    include_reason: bool,
    actor_id: uuid.UUID | None,
    message_id: uuid.UUID,
) -> AiDraft:
    """Ask the model for the personal paragraph (and a reason). Raises
    ``ModelUnavailable`` (fail closed: the person writes it) or ``DraftRefusedError``."""
    if template_key not in AI_TEMPLATES:
        raise DraftRefusedError(["this template has no personal paragraph"])
    evidence = evidence_inputs(summary) if include_reason else []
    inputs = draft_inputs(template_key, job_title, locale, evidence)
    result: RouterResult[MessageDraft] = await router.complete(
        task=TASK,
        inputs=inputs,
        schema=MessageDraft,
        org_id=org_id,
        context=CallContext(
            actor_id=str(actor_id) if actor_id else None,
            workflow=WORKFLOW,
            entity_type="hire.message",
            entity_id=str(message_id),
        ),
    )
    problems = check_draft(result.value, inputs)
    if problems:
        raise DraftRefusedError(problems)
    values = {"personal_note": result.value.personal_note}
    if result.value.reason:
        values["reason"] = result.value.reason
    return AiDraft(
        values=values,
        call_id=result.call_id,
        prompt_version=f"{result.task}/{result.prompt_version}",
        model=result.model,
        from_recording=result.from_recording,
        reason_source="rubric_evidence" if "reason" in values else None,
    )

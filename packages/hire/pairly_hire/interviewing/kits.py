# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""Interview kits (brief 3.7.1): per candidate and stage, questions mapped to rubric
criteria plus probes for weak or unclear evidence from that candidate's screening.

For every question of the stage's scorecard (``plan``), one model call (prompt
``hire_interview_kit/v1`` through the model router) writes the questions, the probes
and what a strong answer contains. What the model sees (``kit_inputs``): the job title,
the stage, the language, the rubric criterion, the planned question and the screening
result for that criterion (``met``, ``partly_met``, ``not_met``, ``unknown``,
``not_screened``). Never the CV, a quote, a name or any contact detail. So a kit is
blind safe by construction, and recorded responses (``recordings``) answer for every
candidate of a demo job.

``check_item`` refuses a draft with an em dash, a link, an email address, a phone
number or a protected characteristic; a refused or failed call falls back to the
template item for that criterion (``template_item``), marked ``source: template``. AI
helps, never blocks: without a model the kit is the template kit.

Stored kits (``hire_interview_kits``) are keyed by the hash of all inputs: the same
plan and screening reuse the kit, a changed one makes a new kit. Screening quotes are
added when a kit is read, only while the application is not blind (``kit_view``).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.errors import NotFoundError
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.types import CallContext, TaskSpec
from pairly_core.notifications.templating import has_em_dash
from pairly_hire.interviewing.models import HireInterviewKit
from pairly_hire.interviewing.plan import LoadedPlan, PlanStage, criteria_by_id, stage_plan
from pairly_hire.pipeline.hooks import ScreeningSummary, application_blind, screening_summary
from pairly_hire.pipeline.records import AppRecord
from pairly_hire.rubric import Criterion

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

TASK: Final = TaskSpec(name="hire_interview_kit", prompt_version="v1")
PROMPT_VERSION: Final = f"{TASK.name}/{TASK.prompt_version}"
TEMPLATE_VERSION: Final = "template/v1"
WORKFLOW: Final = "hire"
RESULTS: Final = ("met", "partly_met", "not_met", "unknown", "not_screened")
WEAK: Final = frozenset({"partly_met", "not_met", "unknown"})
MAX_QUOTES: Final = 2

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class KitItemDraft(BaseModel):
    """What the model returns for one criterion."""

    model_config = ConfigDict(extra="forbid")

    questions: list[_Text] = Field(min_length=1, max_length=2)
    probes: list[_Text] = Field(default_factory=list, max_length=3)
    listen_for: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


# ------------------------------------------------------------------ inputs


def stage_name(stage: PlanStage, locale: str) -> str:
    return stage.name_ar if locale == "ar" and stage.name_ar else stage.name


def kit_inputs(
    *,
    job_title: str,
    stage: str,
    locale: str,
    criterion: Criterion,
    plan_question: str,
    result: str,
) -> dict[str, Any]:
    return {
        "job_title": job_title.strip(),
        "stage": stage,
        "language": locale if locale in {"en", "ar"} else "en",
        "criterion": {
            "name": criterion.name,
            "description": criterion.description,
            "evidence": criterion.evidence,
        },
        "plan_question": plan_question,
        "screening_result": result if result in RESULTS else "unknown",
    }


def result_for(
    summary: ScreeningSummary | None, criterion: Criterion
) -> tuple[str, tuple[str, ...]]:
    """The screening result (and quotes) of a criterion, matched by name or id."""
    if summary is None or not summary.criteria:
        return "not_screened", ()
    for item in summary.criteria:
        if item.criterion in {criterion.name, criterion.id}:
            return (item.result if item.result in RESULTS else "unknown"), item.quotes
    return "unknown", ()


# ------------------------------------------------------------------ checks

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_PROTECTED_EN: Final = (
    "age", "aged", "gender", "male", "female", "nationality", "citizen", "religion",
    "religious", "pregnan", "married", "marital", "children", "family", "health",
    "disab", "illness", "race", "ethnic", "appearance", "accent",
)  # fmt: skip
_PROTECTED_AR: Final = (
    "العمر", "الجنس", "الجنسية", "الدين", "حامل", "متزوج", "الزواج", "أطفال", "الأسرة",
    "الصحة", "إعاقة", "المظهر",
)  # fmt: skip


def _problems(name: str, text: str) -> list[str]:
    found: list[str] = []
    if has_em_dash(text):
        found.append(f"{name}: an em dash")
    if _URL_RE.search(text):
        found.append(f"{name}: a link")
    if _EMAIL_RE.search(text):
        found.append(f"{name}: an email address")
    if _PHONE_RE.search(text):
        found.append(f"{name}: a phone number")
    words = re.findall(r"[a-z]+", text.casefold())
    for stem in _PROTECTED_EN:
        if any(w == stem or (len(stem) > 5 and w.startswith(stem)) for w in words):
            found.append(f"{name}: mentions a protected characteristic ({stem})")
            break
    for stem in _PROTECTED_AR:
        if re.search(rf"(^|\s){re.escape(stem)}", text):
            found.append(f"{name}: mentions a protected characteristic ({stem})")
            break
    return found


def check_item(draft: KitItemDraft) -> list[str]:
    problems: list[str] = []
    for i, q in enumerate(draft.questions):
        problems += _problems(f"questions[{i}]", q)
    for i, p in enumerate(draft.probes):
        problems += _problems(f"probes[{i}]", p)
    if draft.listen_for:
        problems += _problems("listen_for", draft.listen_for)
    return sorted(set(problems))


# ------------------------------------------------------------------ template kit

_PROBES: Final[dict[str, dict[str, list[str]]]] = {
    "en": {
        "met": ["If you did it again, what would you change, and why?"],
        "partly_met": [
            "Can you give a second, recent example? What part of it was yours alone?",
            "How big was the work: how many people, how long, what result?",
        ],
        "not_met": [
            "Have you done this kind of work in another setting, such as a side project, "
            "a course or volunteering? Walk me through it.",
            "How would you get up to speed on this in your first three months?",
        ],
        "unknown": [
            "Your application was not clear on this. Where did you do this work, and how often?",
            "What is the hardest problem of this kind you solved? What did you do?",
        ],
        "not_screened": [],
    },
    "ar": {
        "met": ["لو قمت به مرة أخرى، ماذا ستغير، ولماذا؟"],
        "partly_met": [
            "هل يمكنك إعطاء مثال ثان حديث؟ ما الجزء الذي قمت به بنفسك؟",
            "ما حجم العمل: كم شخصا، وكم من الوقت، وما النتيجة؟",
        ],
        "not_met": [
            "هل قمت بعمل من هذا النوع في سياق آخر، مثل مشروع جانبي أو دورة أو عمل تطوعي؟ "
            "حدثني عنه.",
            "كيف ستكتسب هذه المهارة خلال أشهرك الثلاثة الأولى؟",
        ],
        "unknown": [
            "لم يكن طلبك واضحا في هذا. أين قمت بهذا العمل، وكم مرة؟",
            "ما أصعب مشكلة من هذا النوع حللتها؟ ماذا فعلت؟",
        ],
        "not_screened": [],
    },
}


def scripted_item(inputs: dict[str, Any]) -> KitItemDraft:
    """The deterministic kit item for these inputs: the template kit, and the scripted
    answers of the demo recordings."""
    locale = "ar" if inputs.get("language") == "ar" else "en"
    criterion = inputs["criterion"]
    name = str(criterion["name"]).rstrip(".")
    result = str(inputs.get("screening_result", "unknown"))
    plan_question = str(inputs.get("plan_question") or "").strip()
    if locale == "ar":
        extra = f"ما الذي تعلمته عن {name} من أصعب موقف واجهته؟"
        listen = f"إجابة قوية تتضمن: {criterion['evidence']}"
    else:
        extra = f"What did the hardest situation you faced teach you about {name}?"
        listen = f"A strong answer shows: {criterion['evidence']}"
    questions = [q for q in (plan_question, extra) if q][:2]
    return KitItemDraft(
        questions=[q[:300] for q in questions],
        probes=list(_PROBES[locale].get(result, [])),
        listen_for=listen[:300],
    )


# ------------------------------------------------------------------ kits


@dataclass(frozen=True, slots=True)
class KitItem:
    criterion_id: str
    criterion_name: str
    kind: str
    screening_result: str
    plan_question: str
    questions: list[str]
    probes: list[str]
    listen_for: str
    anchors: list[str]
    source: str
    # Screening quotes: only while the application is not blind, never stored.
    quotes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Kit:
    application_id: uuid.UUID
    job_id: uuid.UUID
    stage_key: str
    stage_name: str
    locale: str
    items: list[KitItem]
    source: str
    prompt_version: str
    from_recording: bool
    stored: bool
    blind: bool


@dataclass(frozen=True, slots=True)
class _Planned:
    criterion: Criterion
    kind: str
    question: str
    anchors: list[str]
    result: str
    quotes: tuple[str, ...]
    inputs: dict[str, Any]


def _hash(parts: list[dict[str, Any]]) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _planned(
    session: AsyncSession, record: AppRecord, stage: PlanStage, loaded: LoadedPlan, locale: str
) -> list[_Planned]:
    summary = await screening_summary(session, record.app.id)
    known = criteria_by_id(loaded.rubric)
    title = record.job.title_ar if locale == "ar" and record.job.title_ar else record.job.title
    out: list[_Planned] = []
    for question in stage.questions:
        found = known.get(question.criterion_id)
        if found is None:
            continue
        criterion, kind = found
        result, quotes = result_for(summary, criterion)
        text = (
            question.question_ar if locale == "ar" and question.question_ar else question.question
        )
        out.append(
            _Planned(
                criterion=criterion,
                kind=kind,
                question=text,
                anchors=list(question.scale(locale)),
                result=result,
                quotes=quotes,
                inputs=kit_inputs(
                    job_title=title,
                    stage=stage_name(stage, locale),
                    locale=locale,
                    criterion=criterion,
                    plan_question=text,
                    result=result,
                ),
            )
        )
    return out


def _item(p: _Planned, draft: KitItemDraft, source: str, blind: bool) -> KitItem:
    return KitItem(
        criterion_id=p.criterion.id,
        criterion_name=p.criterion.name,
        kind=p.kind,
        screening_result=p.result,
        plan_question=p.question,
        questions=list(draft.questions),
        probes=list(draft.probes),
        listen_for=draft.listen_for,
        anchors=p.anchors,
        source=source,
        quotes=[] if blind else list(p.quotes[:MAX_QUOTES]),
    )


def _stored_item(p: _Planned, data: dict[str, Any], blind: bool) -> KitItem:
    return _item(
        p,
        KitItemDraft(
            questions=list(data.get("questions") or [p.question]),
            probes=list(data.get("probes") or []),
            listen_for=str(data.get("listen_for") or ""),
        ),
        str(data.get("source") or "model"),
        blind,
    )


async def build_kit(
    session: AsyncSession,
    record: AppRecord,
    stage_key: str,
    locale: str,
    *,
    router: ModelRouter | None,
    actor_id: uuid.UUID | None = None,
) -> Kit:
    """The kit of an application at a stage: the stored one for these inputs, else a new
    one from the model (stored), else the template kit (not stored)."""
    locale = "ar" if locale == "ar" else "en"
    found = await stage_plan(session, record.job, stage_key)
    if found is None:
        raise NotFoundError("this stage has no interview plan")
    stage, loaded = found
    # Quotes from the CV only once the candidate's identity is shown at this stage.
    blind = record.identity_hidden(await application_blind(session, record.app.id))
    planned = await _planned(session, record, stage, loaded, locale)
    digest = _hash([p.inputs | {"criterion_id": p.criterion.id} for p in planned])
    row = await session.scalar(
        select(HireInterviewKit).where(
            HireInterviewKit.application_id == record.app.id,
            HireInterviewKit.stage_key == stage_key,
            HireInterviewKit.locale == locale,
            HireInterviewKit.inputs_hash == digest,
        )
    )
    if row is not None:
        by_id = {str(i.get("criterion_id")): i for i in row.items if isinstance(i, dict)}
        items = [_stored_item(p, by_id.get(p.criterion.id, {}), blind) for p in planned]
        return Kit(
            record.app.id,
            record.job.id,
            stage_key,
            stage_name(stage, locale),
            locale,
            items,
            _overall(items),
            row.prompt_version,
            row.from_recording,
            True,
            blind,
        )
    if router is None:
        items = [_item(p, scripted_item(p.inputs), "template", blind) for p in planned]
        return Kit(
            record.app.id,
            record.job.id,
            stage_key,
            stage_name(stage, locale),
            locale,
            items,
            "template",
            TEMPLATE_VERSION,
            False,
            False,
            blind,
        )
    items = []
    call_ids: list[uuid.UUID] = []
    recorded = True
    for p in planned:
        try:
            result = await router.complete(
                task=TASK,
                inputs=p.inputs,
                schema=KitItemDraft,
                org_id=record.app.org_id,
                context=CallContext(
                    actor_id=str(actor_id) if actor_id else None,
                    workflow=WORKFLOW,
                    entity_type="hire.application",
                    entity_id=str(record.app.id),
                ),
            )
        except ModelUnavailable:
            items.append(_item(p, scripted_item(p.inputs), "template", blind))
            recorded = False
            continue
        call_ids.append(result.call_id)
        recorded = recorded and result.from_recording
        if check_item(result.value):
            items.append(_item(p, scripted_item(p.inputs), "template", blind))
            continue
        items.append(_item(p, result.value, "model", blind))
    source = _overall(items)
    if source == "template":
        # Nothing came from the model: do not keep it, so a later read tries again.
        return Kit(
            record.app.id,
            record.job.id,
            stage_key,
            stage_name(stage, locale),
            locale,
            items,
            source,
            TEMPLATE_VERSION,
            False,
            False,
            blind,
        )
    stored = HireInterviewKit(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        job_id=record.job.id,
        stage_key=stage_key,
        locale=locale,
        inputs_hash=digest,
        items=[
            {
                "criterion_id": i.criterion_id,
                "questions": i.questions,
                "probes": i.probes,
                "listen_for": i.listen_for,
                "source": i.source,
            }
            for i in items
        ],
        prompt_version=PROMPT_VERSION,
        model_call_ids=call_ids,
        from_recording=recorded,
    )
    try:
        async with session.begin_nested():
            session.add(stored)
            await session.flush()
            await append_audit(
                session,
                action="hire.interview_kit.generated",
                actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
                actor_id=str(actor_id) if actor_id else "pairly.hire.kits",
                entity_type="hire.application",
                entity_id=record.app.id,
                workflow=WORKFLOW,
                payload={
                    "kit_id": str(stored.id),
                    "stage_key": stage_key,
                    "locale": locale,
                    "source": source,
                    "prompt_version": PROMPT_VERSION,
                    "model_call_ids": [str(c) for c in call_ids],
                    "from_recording": recorded,
                },
            )
    except IntegrityError:
        pass  # another request stored the same kit: both are the same inputs
    return Kit(
        record.app.id,
        record.job.id,
        stage_key,
        stage_name(stage, locale),
        locale,
        items,
        source,
        PROMPT_VERSION,
        recorded,
        True,
        blind,
    )


def _overall(items: list[KitItem]) -> str:
    sources = {i.source for i in items}
    if sources == {"model"}:
        return "model"
    if "model" in sources:
        return "partial"
    return "template"


def kit_text(kit: Kit, *, max_chars: int = 2400) -> str:
    """The kit as plain text for the interviewer briefing email (no quotes ever)."""
    ar = kit.locale == "ar"
    lines: list[str] = []
    for n, item in enumerate(kit.items, start=1):
        lines.append(f"{n}. {item.criterion_name}")
        for q in item.questions:
            lines.append(f"   {'سؤال' if ar else 'Ask'}: {q}")
        for p in item.probes:
            lines.append(f"   {'تعمق' if ar else 'Probe'}: {p}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."

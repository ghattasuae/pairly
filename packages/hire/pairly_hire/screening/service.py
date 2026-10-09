# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Screening rows, settings, decision records and their replay handlers.

* ``locked_screening`` / ``add_reasons`` / ``read_details`` / ``write_details``: row
  helpers (``details`` is encrypted; read, change, seal again).
* ``screening_settings``: the organisation's blind mode, university names, gap months
  and classifier mode, with the defaults when it never saved any.
* Decision records (brief 2.3, hard rule 5), on entity ``hire.application``:
  - ``record_score``: kind ``score``. The credited criterion scores and the overall
    score, the locked rubric version (``rubric_version_id`` and a policy reference
    with its hash), the prompt version, the model calls, the evidence locations (the
    quotes themselves are personal data and go in ``sensitive``), the claim and AI
    flags, the guardrail findings and the blind status. Replay handler
    ``hire.score_aggregation``.
  - ``record_suggestion``: kind ``shortlist_suggestion``. The knockout results with
    their suggested actions, the review reasons and the flags: what the person is
    asked to look at. Never an outcome. Replay handler ``hire.screening_suggestion``.
  - ``record_override``: kind ``human_override`` (a human kind that stays replayable,
    ``REPLAYABLE_HUMAN_KINDS``), superseding the newest record, with the person's
    reason (encrypted). Same replay handler: replaying it recomputes the overall score
    from the overridden scores and the rubric.
* Replay handlers (``pairly_core.decisions.replay.register_handler``), registered when
  this module is imported (the plugin imports it):
  - ``hire.score_aggregation``: re-reads the rubric version the record names (its hash
    must match), recomputes every credited score from the stored model score and the
    number of accepted evidence quotes (no evidence, no credit), and the overall score
    with ``aggregate.overall_score``. A replayed model output (recorded or live) is
    compared criterion by criterion as well.
  - ``hire.screening_suggestion``: recomputes the knockout routing from the stored
    answers and the rubric.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.crypto.types import Plaintext, seal, unseal
from pairly_core.decisions.models import DecisionKind, DecisionRecord
from pairly_core.decisions.replay import (
    HandlerInput,
    HandlerResult,
    ReplayFailedError,
    register_handler,
    register_schema,
)
from pairly_core.decisions.service import (
    DecisionDraft,
    PolicyRef,
    PromptRef,
    StoredDecision,
    chain_head,
    record_decision,
)
from pairly_core.errors import NotFoundError
from pairly_core.policy_versions import PolicyError, get_policy
from pairly_hire.reveal import revealed_ids
from pairly_hire.rubric import POLICY_KIND, Rubric, parse_rubric
from pairly_hire.scoring.aggregate import overall_score
from pairly_hire.scoring.model import CvScoreOutput
from pairly_hire.screening.blind import BlindConfig
from pairly_hire.screening.claims import ClaimConfig
from pairly_hire.screening.knockouts import check_knockouts
from pairly_hire.screening.models import (
    DEFAULT_GAP_MONTHS,
    HireScreening,
    HireScreeningSettings,
)

WORKFLOW: Final = "hire"
ENTITY_TYPE: Final = "hire.application"
SCREENING_ENTITY: Final = "hire.screening"
HANDLER_SCORE: Final = "hire.score_aggregation"
HANDLER_SUGGESTION: Final = "hire.screening_suggestion"
MAX_REASONS: Final = 50


class ScreeningMissingError(NotFoundError):
    """The screening of a run is gone or belongs to another organisation."""


async def locked_screening(session: AsyncSession, screening_id: uuid.UUID) -> HireScreening:
    row = await session.scalar(
        select(HireScreening)
        .where(HireScreening.id == screening_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ScreeningMissingError("screening not found")
    return row


def add_reasons(row: HireScreening, reasons: Iterable[str]) -> None:
    merged = list(dict.fromkeys([*row.review_reasons, *(r[:200] for r in reasons)]))
    row.review_reasons = merged[:MAX_REASONS]


def read_details(row: HireScreening) -> dict[str, Any]:
    current: Any = row.details
    if current is None:
        return {}
    # A value written earlier in this transaction is still the plaintext to seal.
    value = current.value if isinstance(current, Plaintext) else unseal(row, "details")
    return dict(value) if isinstance(value, Mapping) else {}


def write_details(row: HireScreening, **changes: Any) -> None:
    details = read_details(row)
    details.update(changes)
    sealed: Any = seal(HireScreening, "details", details, org_id=row.org_id, row_id=row.id)
    row.details = sealed


def touch(row: HireScreening) -> None:
    row.updated_at = datetime.now(UTC)


async def is_blind(session: AsyncSession, application_id: uuid.UUID) -> bool:
    """Whether reviewers must not see the candidate's name and contact details now
    (``blind_application_ids`` for one application)."""
    return application_id in await blind_application_ids(session, [application_id])


async def blind_application_ids(
    session: AsyncSession, application_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """The ids that are blind for reviewers now, in two queries: blind mode applies (the
    latest screening ran blind, or there is none yet and the organisation's blind mode
    is on) and the application's one reveal state is not set (``pairly_hire.reveal``:
    the shortlist approval or a move into a stage that reveals identity)."""
    ids = list(dict.fromkeys(application_ids))
    if not ids:
        return set()
    rows = await session.execute(
        select(HireScreening.application_id, HireScreening.blind)
        .where(HireScreening.application_id.in_(ids))
        .ext(distinct_on(HireScreening.application_id))
        .order_by(HireScreening.application_id, HireScreening.created_at.desc())
    )
    latest = {r.application_id: bool(r.blind) for r in rows}
    unscreened = (
        (await screening_settings(session)).blind.enabled if len(latest) < len(ids) else False
    )
    applies = [i for i in ids if latest.get(i, unscreened)]
    if not applies:
        return set()
    return set(applies) - await revealed_ids(session, applies)


# ------------------------------------------------------------------ settings


@dataclass(frozen=True, slots=True)
class ScreeningConfig:
    blind: BlindConfig
    claims: ClaimConfig
    classifier_mode: str


DEFAULT_CONFIG: Final = ScreeningConfig(BlindConfig(), ClaimConfig(), "always")


async def screening_settings(session: AsyncSession) -> ScreeningConfig:
    row = await session.scalar(select(HireScreeningSettings))
    if row is None:
        return DEFAULT_CONFIG
    return ScreeningConfig(
        blind=BlindConfig(enabled=row.blind_mode, universities=row.blind_universities),
        claims=ClaimConfig(gap_months=row.gap_months or DEFAULT_GAP_MONTHS),
        classifier_mode=row.classifier_mode,
    )


# ------------------------------------------------------------------ rubric references


async def rubric_ref(session: AsyncSession, policy_id: uuid.UUID) -> tuple[PolicyRef, Rubric]:
    policy = await get_policy(session, policy_id)
    if policy is None or policy.kind != POLICY_KIND:
        raise PolicyError("the application has no locked rubric version")
    return (
        PolicyRef(policy.id, policy.kind, policy.name, policy.version, policy.content_hash),
        parse_rubric(policy.content),
    )


async def refresh_decision_ids(session: AsyncSession, row: HireScreening) -> None:
    """Every decision record of this screening: its own records (score, suggestion,
    overrides name the screening in ``inputs``), the approval and final action of its
    approval, and the extraction of its CV parse."""
    mine = [DecisionRecord.inputs["screening_id"].astext == str(row.id)]
    if row.approval_id is not None:
        mine.append(DecisionRecord.approval_ids.contains([row.approval_id]))
    rows = await session.scalars(
        select(DecisionRecord.id)
        .where(
            DecisionRecord.workflow == WORKFLOW,
            or_(
                and_(
                    DecisionRecord.entity_type == ENTITY_TYPE,
                    DecisionRecord.entity_id == str(row.application_id),
                    or_(*mine),
                ),
                and_(
                    DecisionRecord.entity_type == "document",
                    DecisionRecord.entity_id == str(row.document_id),
                    DecisionRecord.inputs["extraction_id"].astext == str(row.extraction_id),
                ),
            ),
        )
        .order_by(DecisionRecord.decided_at, DecisionRecord.id)
    )
    row.decision_ids = list(rows)


# ------------------------------------------------------------------ decision records


def score_output(scores: Mapping[str, int], overall: float) -> dict[str, Any]:
    return {"scores": dict(scores), "overall": overall}


async def record_score(
    session: AsyncSession,
    *,
    row: HireScreening,
    rubric: PolicyRef,
    criteria_public: Sequence[Mapping[str, Any]],
    scores: Mapping[str, int],
    overall: float,
    sensitive: Mapping[str, Any],
    prompt: PromptRef,
    model: Mapping[str, Any],
    call_ids: Sequence[uuid.UUID],
    flags: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    actor_id: str,
    confidence: float | None,
) -> StoredDecision:
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.SCORE,
            workflow=WORKFLOW,
            entity_type=ENTITY_TYPE,
            entity_id=str(row.application_id),
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            replay_handler=HANDLER_SCORE,
            inputs={
                "screening_id": str(row.id),
                "document_id": str(row.document_id),
                "sanitized_sha256": row.sanitized_sha256,
                "blinded_sha256": row.blinded_sha256,
                "blind": row.blind,
                "blind_config": dict(row.blind_config),
            },
            policy_versions=[rubric],
            rubric_version_id=rubric.id,
            prompt_versions=[prompt],
            model_call_ids=list(call_ids),
            model=dict(model),
            output=score_output(scores, overall),
            confidence=confidence,
            evidence={
                "criteria": [dict(c) for c in criteria_public],
                "flags": [
                    {"code": f["code"], "kind": f["kind"], "severity": f.get("severity", "info")}
                    for f in flags
                ],
                "guardrail_findings": [dict(f) for f in findings],
                "blind_categories": dict(row.blind_config.get("categories", {})),
            },
            sensitive=dict(sensitive),
        ),
    )


async def record_suggestion(
    session: AsyncSession,
    *,
    row: HireScreening,
    rubric: PolicyRef,
    knockouts: Sequence[Mapping[str, Any]],
    answers: Mapping[str, str],
    flags: Sequence[Mapping[str, Any]],
    actor_id: str,
) -> StoredDecision:
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.SHORTLIST_SUGGESTION,
            workflow=WORKFLOW,
            entity_type=ENTITY_TYPE,
            entity_id=str(row.application_id),
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            replay_handler=HANDLER_SUGGESTION,
            inputs={"screening_id": str(row.id), "score_decision_id": row.score_decision_id},
            policy_versions=[rubric],
            rubric_version_id=rubric.id,
            output={
                # Every screening goes to a person; the suggestion is what to look at.
                "route": "human_review",
                "knockouts": [
                    {
                        "id": k["id"],
                        "matches": k["matches"],
                        "route": k["route"],
                        "suggested_action": k["suggested_action"],
                    }
                    for k in knockouts
                ],
                "review_reasons": list(row.review_reasons),
                "overall": float(row.overall_score) if row.overall_score is not None else None,
            },
            evidence={"flags": [{"code": f["code"], "kind": f["kind"]} for f in flags]},
            sensitive={"knockout_answers": dict(answers)},
        ),
    )


async def record_override(
    session: AsyncSession,
    *,
    row: HireScreening,
    rubric: PolicyRef,
    scores: Mapping[str, int],
    overall: float,
    changed: Mapping[str, Mapping[str, int | None]],
    reason: str,
    actor_type: ActorType,
    actor_id: str,
) -> StoredDecision:
    """A person's override, superseding the newest record of the application."""
    head = await chain_head(session, ENTITY_TYPE, str(row.application_id))
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.HUMAN_OVERRIDE,
            workflow=WORKFLOW,
            entity_type=ENTITY_TYPE,
            entity_id=str(row.application_id),
            actor_type=actor_type,
            actor_id=actor_id,
            replay_handler=HANDLER_SCORE,
            inputs={"screening_id": str(row.id), "override_of": row.score_decision_id},
            policy_versions=[rubric],
            rubric_version_id=rubric.id,
            output={**score_output(scores, overall), "override": True},
            evidence={
                "changed": {k: dict(v) for k, v in changed.items()},
                "credited_as_given": True,
            },
            reason=reason,
            supersedes=head,
        ),
    )


# ------------------------------------------------------------------ replay handlers


async def _rubric_of(inp: HandlerInput) -> Rubric:
    refs = [p for p in inp.record.policy_versions if p.get("kind") == POLICY_KIND]
    if not refs:
        raise ReplayFailedError("policy_version_missing")
    policy = await get_policy(inp.session, uuid.UUID(str(refs[0]["id"])))
    if policy is None:
        raise ReplayFailedError("policy_version_missing")
    if policy.content_hash != refs[0].get("hash"):
        raise ReplayFailedError("policy_version_hash_mismatch")
    try:
        return parse_rubric(policy.content)
    except PolicyError:
        raise ReplayFailedError("stored_policy_invalid") from None


async def replay_score(inp: HandlerInput) -> HandlerResult:
    rubric = await _rubric_of(inp)
    output = inp.record.output
    stored = output.get("scores")
    if not isinstance(stored, Mapping):
        raise ReplayFailedError("stored_inputs_invalid")
    details: dict[str, Any] = {}
    if inp.record.kind == DecisionKind.HUMAN_OVERRIDE.value or output.get("override"):
        replayed_scores = {str(k): int(v) for k, v in stored.items()}
    else:
        criteria = inp.record.evidence.get("criteria")
        if not isinstance(criteria, list):
            raise ReplayFailedError("stored_inputs_invalid")
        replayed_scores = {}
        model_scores: dict[str, int | None] = {}
        for item in criteria:
            if not isinstance(item, Mapping):
                raise ReplayFailedError("stored_inputs_invalid")
            model_score = item.get("model_score")
            evidenced = len(item.get("evidence") or []) > 0
            credited = int(model_score) if isinstance(model_score, int) and evidenced else 0
            replayed_scores[str(item["criterion_id"])] = credited
            model_scores[str(item["criterion_id"])] = (
                int(model_score) if isinstance(model_score, int) else None
            )
        # A replayed model call (recorded or live) is compared with the stored answer.
        outputs = [out for _, out in inp.model_outputs if isinstance(out, dict)]
        if outputs:
            try:
                replayed = CvScoreOutput.model_validate(outputs[-1])
            except ValidationError:
                raise ReplayFailedError("model_output_invalid") from None
            again = {c.criterion_id: c.score for c in replayed.criteria}
            details["model_scores_match"] = all(
                again.get(k) == v for k, v in model_scores.items() if v is not None
            )
    try:
        overall = overall_score(rubric, replayed_scores)
    except ValueError:
        raise ReplayFailedError("stored_inputs_invalid") from None
    original = {"scores": dict(stored), "overall": output.get("overall")}
    replayed_out = {"scores": replayed_scores, "overall": overall}
    return HandlerResult(original, replayed_out, details=details)


async def replay_suggestion(inp: HandlerInput) -> HandlerResult:
    rubric = await _rubric_of(inp)
    answers = inp.sensitive.get("knockout_answers")
    if not isinstance(answers, Mapping):
        raise ReplayFailedError("stored_inputs_invalid")
    checks = check_knockouts(rubric, {str(k): str(v) for k, v in answers.items()})
    stored = inp.record.output.get("knockouts") or []
    original = {
        "route": inp.record.output.get("route"),
        "knockouts": {k["id"]: k["route"] for k in stored if isinstance(k, Mapping)},
    }
    replayed = {
        "route": "human_review",
        "knockouts": {c.id: "pass" if c.matches else "human_review" for c in checks},
    }
    return HandlerResult(original, replayed)


def _register() -> None:
    register_handler(HANDLER_SCORE, replay_score)
    register_handler(HANDLER_SUGGESTION, replay_suggestion)
    register_schema(CvScoreOutput)


_register()

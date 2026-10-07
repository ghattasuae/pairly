# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Rule sets over ``policy_versions`` and the ``rule_results`` record.

* ``publish_rule_set``: validate and store a new immutable version (never an edit).
* ``load_rule_set``: the latest or an exact version, parsed.
* ``evaluate_and_record``: evaluate a rule set against facts and store every rule's
  result with its reason, the facts it read and the policy version, plus one audit
  event, all in the caller's transaction.
* ``dry_run``: evaluate without storing anything (for the rule editor).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.models.policy import PolicyVersion
from pairly_core.policy.models import ResultSource, RuleResult
from pairly_core.policy.rules import (
    RULES_KIND,
    Outcome,
    RuleSet,
    RuleSetEvaluation,
    evaluate_rule_set,
    parse_rule_set,
    rule_set_validator,
)
from pairly_core.policy_versions import (
    PolicyError,
    PublishedPolicy,
    get_policy,
    latest_policy,
    publish_policy_version,
)

RULES_EVALUATED: Final = "core.policy.rules_evaluated"


class RuleSetNotFoundError(PolicyError):
    """No published rule set (or version) with this name. Callers fail closed."""


@dataclass(frozen=True, slots=True)
class Subject:
    """What was evaluated (an invoice, a candidate), by reference only."""

    type: str | None = None
    id: str | None = None
    workflow: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedEvaluation:
    evaluation_id: uuid.UUID
    policy: PublishedPolicy
    evaluation: RuleSetEvaluation

    @property
    def overall(self) -> Outcome:
        return self.evaluation.overall


async def publish_rule_set(
    session: AsyncSession,
    *,
    name: str,
    content: str,
    actor_type: ActorType,
    actor_id: str,
) -> PublishedPolicy:
    return await publish_policy_version(
        session,
        kind=RULES_KIND,
        name=name,
        content=content,
        validate=rule_set_validator(name),
        actor_type=actor_type,
        actor_id=actor_id,
    )


async def load_rule_set(
    session: AsyncSession, name: str, version: int | None = None
) -> tuple[PublishedPolicy, RuleSet]:
    if version is None:
        policy = await latest_policy(session, RULES_KIND, name)
    else:
        row = await session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.kind == RULES_KIND,
                PolicyVersion.name == name,
                PolicyVersion.version == version,
            )
        )
        policy = PublishedPolicy.from_row(row) if row is not None else None
    if policy is None:
        raise RuleSetNotFoundError("no published rule set with this name and version")
    # Stored content was validated when published; parse again (the parser may be newer).
    return policy, parse_rule_set(policy.content)


async def load_rule_set_by_id(
    session: AsyncSession, policy_version_id: uuid.UUID
) -> tuple[PublishedPolicy, RuleSet]:
    policy = await get_policy(session, policy_version_id)
    if policy is None or policy.kind != RULES_KIND:
        raise RuleSetNotFoundError("no published rule set with this id")
    return policy, parse_rule_set(policy.content)


def dry_run(rule_set: RuleSet, facts: Mapping[str, Any], *, as_of: date) -> RuleSetEvaluation:
    return evaluate_rule_set(rule_set, facts, as_of=as_of)


async def evaluate_and_record(
    session: AsyncSession,
    *,
    name: str,
    facts: Mapping[str, Any],
    actor_type: ActorType,
    actor_id: str,
    subject: Subject | None = None,
    version: int | None = None,
    as_of: date | None = None,
) -> RecordedEvaluation:
    """Evaluate the rule set ``name`` (latest, or ``version``) and store every result."""
    org = current_org(session)
    if org is None:
        raise TenantError("policy evaluation needs a tenant context")
    subject = subject or Subject()
    policy, rule_set = await load_rule_set(session, name, version)
    as_of = as_of or datetime.now(UTC).date()
    evaluation = evaluate_rule_set(rule_set, facts, as_of=as_of)
    evaluation_id = uuid.uuid4()
    rows: list[dict[str, Any]] = [
        {
            "org_id": org,
            "evaluation_id": evaluation_id,
            "source": ResultSource.RULES.value,
            "policy_version_id": policy.id,
            "policy_name": policy.name,
            "policy_version": policy.version,
            "rule_id": result.rule_id,
            "outcome": result.outcome.value,
            "severity": result.severity.value,
            "reason": result.reason,
            "reason_ar": result.reason_ar,
            "error_code": result.error_code,
            "facts": result.facts,
            "evidence": [],
            "as_of": as_of.isoformat(),
            "workflow": subject.workflow,
            "subject_type": subject.type,
            "subject_id": subject.id,
            "created_by_type": actor_type.value,
            "created_by_id": actor_id,
        }
        for result in evaluation.results
    ]
    await session.execute(insert(RuleResult), rows)
    await append_audit(
        session,
        action=RULES_EVALUATED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=subject.type,
        entity_id=subject.id,
        workflow=subject.workflow,
        payload={
            "evaluation_id": str(evaluation_id),
            "policy_version_id": str(policy.id),
            "policy": policy.name,
            "version": policy.version,
            "overall": evaluation.overall.value,
            "counts": _counts(evaluation),
        },
    )
    return RecordedEvaluation(evaluation_id=evaluation_id, policy=policy, evaluation=evaluation)


def _counts(evaluation: RuleSetEvaluation) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in evaluation.results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    return counts


async def results_for_evaluation(
    session: AsyncSession, evaluation_id: uuid.UUID
) -> Sequence[RuleResult]:
    rows = await session.scalars(
        select(RuleResult)
        .where(RuleResult.evaluation_id == evaluation_id)
        .order_by(RuleResult.created_at, RuleResult.rule_id)
    )
    return list(rows)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Appeals and human review requests (brief 2.3.3).

A candidate or vendor contests one decision by its id. Until the portals exist (M2,
M4), staff file the appeal on their behalf (``filed_by``). An appeal has an SLA
(``sla_due_at``), stays in the queue until a person resolves it with a written outcome,
and is audited at every step:

* ``create_appeal``: ``core.appeal.created``.
* ``resolve_appeal``: ``core.appeal.resolved``, plus a decision record (kind
  ``appeal_outcome``) that supersedes the head of the appealed decision's line, so the
  history shows "AI said, a person decided, the appeal reviewed it". Segregation of
  duties: the person who filed the appeal, and the person whose decision is appealed,
  cannot resolve it.
* ``escalate_overdue_appeals``: the worker's SLA job (same pattern as approvals).
  Marks overdue open appeals escalated, once, with a ``core.appeal.escalated`` event.

The reason and the outcome text are free text from people, so both are encrypted.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, actor_ref, append_audit
from pairly_core.crypto.types import seal, unseal
from pairly_core.db import TenantError, current_org
from pairly_core.decisions.models import (
    Appeal,
    AppealOutcome,
    AppealStatus,
    DecisionKind,
    DecisionRecord,
    RequesterType,
)
from pairly_core.decisions.permissions import APPEAL_READ, DECISION_READ
from pairly_core.decisions.scope import (
    WorkflowScope,
    authorize_appeal_decision,
    decide_scope,
    read_scope,
)
from pairly_core.decisions.service import (
    DecisionDraft,
    chain_head,
    get_record,
    record_decision,
)
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.rbac import Actor, ActorKind, Denial, PermissionDeniedError

APPEAL_CREATED: Final = "core.appeal.created"
APPEAL_RESOLVED: Final = "core.appeal.resolved"
APPEAL_ESCALATED: Final = "core.appeal.escalated"
SYSTEM_ACTOR_ID: Final = "pairly.appeals.sla"
DEFAULT_SLA: Final = timedelta(days=14)
MAX_REASON: Final = 4000
_REQUESTER_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class AppealView:
    id: uuid.UUID
    decision_id: str
    requester_type: str
    requester_id: str
    filed_by: uuid.UUID | None
    reason: str
    status: str
    outcome: str | None
    outcome_text: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    outcome_decision_id: str | None
    sla_due_at: datetime
    escalated_at: datetime | None
    created_at: datetime
    # The appealed decision's workflow (scoping, pairly_core.decisions.scope).
    workflow: str | None = None

    @classmethod
    def of(cls, row: Appeal, workflow: str | None = None) -> AppealView:
        return cls(
            id=row.id,
            decision_id=row.decision_id,
            requester_type=row.requester_type,
            requester_id=row.requester_id,
            filed_by=row.filed_by,
            reason=str(unseal(row, "reason")),
            status=row.status,
            outcome=row.outcome,
            outcome_text=unseal(row, "outcome_text"),
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            outcome_decision_id=row.outcome_decision_id,
            sla_due_at=row.sla_due_at,
            escalated_at=row.escalated_at,
            created_at=row.created_at,
            workflow=workflow,
        )


def clean_text(value: str, what: str) -> str:
    cleaned = _CONTROL_RE.sub("", value or "").strip()
    if not cleaned:
        raise BadRequestError(f"{what} is required")
    if len(cleaned) > MAX_REASON:
        raise BadRequestError(f"{what} is longer than {MAX_REASON} characters")
    return cleaned


async def _now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(text("now()")))
    if not isinstance(value, datetime):  # pragma: no cover - Postgres always answers
        raise RuntimeError("no database time")
    return value


async def create_appeal(
    session: AsyncSession,
    actor: Actor,
    *,
    decision_id: str,
    requester_type: RequesterType,
    requester_id: str,
    reason: str,
    sla: timedelta = DEFAULT_SLA,
) -> AppealView:
    """File an appeal against ``decision_id`` (staff on behalf of the requester)."""
    org = current_org(session)
    if org is None or org != actor.org_id:
        raise TenantError("appeals need the actor's tenant context")
    record = await get_record(session, decision_id)
    if record is None or not read_scope(actor, DECISION_READ).allows(record.workflow):
        raise NotFoundError("decision not found")
    if not _REQUESTER_RE.fullmatch(requester_id):
        raise BadRequestError("invalid requester id")
    clean_reason = clean_text(reason, "a reason")
    requester = RequesterType(requester_type)
    appeal_id = uuid.uuid4()
    now = await _now(session)
    session.add(
        Appeal(
            id=appeal_id,
            org_id=org,
            decision_id=record.id,
            requester_type=requester.value,
            requester_id=requester_id,
            filed_by=actor.id if actor.kind is ActorKind.USER else None,
            reason=seal(Appeal, "reason", clean_reason, org_id=org, row_id=appeal_id),
            status=AppealStatus.OPEN.value,
            sla_due_at=now + sla,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action=APPEAL_CREATED,
        actor_type=kind,
        actor_id=actor_id,
        entity_type="appeal",
        entity_id=appeal_id,
        workflow=record.workflow,
        payload={
            "decision_id": record.id,
            "requester_type": requester.value,
            "sla_due_at": now.isoformat(),
        },
    )
    row = await session.scalar(select(Appeal).where(Appeal.id == appeal_id))
    if row is None:  # pragma: no cover - RLS would have refused the insert
        raise TenantError("appeal not visible after insert")
    return AppealView.of(row, record.workflow)


def appeal_scope(actor: Actor) -> WorkflowScope:
    """Workflows whose appeals ``actor`` sees: readers of the workflow and its deciders."""
    return read_scope(actor, APPEAL_READ).union(decide_scope(actor))


async def get_appeal(
    session: AsyncSession, appeal_id: uuid.UUID, scope: WorkflowScope | None = None
) -> AppealView | None:
    """One appeal, or None when it does not exist or lies outside ``scope``."""
    found = (
        await session.execute(
            select(Appeal, DecisionRecord.workflow)
            .join(DecisionRecord, DecisionRecord.id == Appeal.decision_id)
            .where(Appeal.id == appeal_id)
        )
    ).first()
    if found is None:
        return None
    row, workflow = found
    if scope is not None and not scope.allows(workflow):
        return None
    return AppealView.of(row, workflow)


async def list_appeals(
    session: AsyncSession,
    *,
    scope: WorkflowScope,
    status: AppealStatus | None = None,
    limit: int = 100,
) -> list[AppealView]:
    """The appeals queue (by SLA due date), limited to the workflows in ``scope``."""
    query = (
        select(Appeal, DecisionRecord.workflow)
        .join(DecisionRecord, DecisionRecord.id == Appeal.decision_id)
        .where(scope.clause(DecisionRecord.workflow))
        .order_by(Appeal.sla_due_at, Appeal.id)
        .limit(max(1, min(limit, 200)))
    )
    if status is not None:
        query = query.where(Appeal.status == status.value)
    return [AppealView.of(row, workflow) for row, workflow in await session.execute(query)]


def _deny(actor: Actor, reason: str) -> PermissionDeniedError:
    return PermissionDeniedError(
        "you cannot resolve this appeal",
        denial=Denial(actor=actor, permission="core.appeal.decide", reason=reason),
    )


async def resolve_appeal(
    session: AsyncSession,
    actor: Actor,
    appeal_id: uuid.UUID,
    *,
    outcome: AppealOutcome,
    outcome_text: str,
) -> AppealView:
    """Close an appeal with a written outcome and record it as a decision."""
    if actor.kind is not ActorKind.USER:
        raise BadRequestError("only a person can resolve an appeal")
    row = await session.scalar(select(Appeal).where(Appeal.id == appeal_id).with_for_update())
    if row is None:
        raise NotFoundError("appeal not found")
    if row.status != AppealStatus.OPEN.value:
        raise ConflictError("the appeal is already resolved")
    text_value = clean_text(outcome_text, "a written outcome")
    record = await session.scalar(
        select(DecisionRecord).where(DecisionRecord.id == row.decision_id)
    )
    if record is None:  # pragma: no cover - foreign key
        raise NotFoundError("decision not found")
    # Outside the actor's workflows the appeal does not exist for them; inside, they need
    # the workflow's decide permission (or core.appeal.decide) and a second factor.
    if not appeal_scope(actor).allows(record.workflow):
        raise NotFoundError("appeal not found")
    authorize_appeal_decision(actor, record.workflow)
    if row.filed_by == actor.id:
        raise _deny(actor, "segregation_of_duties_filer")
    if record.actor_type == ActorType.USER.value and record.actor_id == str(actor.id):
        raise _deny(actor, "segregation_of_duties_decider")
    now = await _now(session)
    head = await chain_head(session, record.entity_type, record.entity_id)
    stored = await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.APPEAL_OUTCOME,
            workflow=record.workflow,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            actor_type=ActorType.USER,
            actor_id=str(actor.id),
            inputs={"appeal_id": str(row.id), "appealed_decision_id": record.id},
            output={"outcome": outcome.value, "appealed_decision_id": record.id},
            final_action=f"appeal_{outcome.value}",
            reason=text_value,
            decided_at=now,
            supersedes=head,
        ),
    )
    await session.execute(
        update(Appeal)
        .where(Appeal.id == row.id)
        .values(
            status=AppealStatus.RESOLVED.value,
            outcome=outcome.value,
            outcome_text=seal(Appeal, "outcome_text", text_value, org_id=row.org_id, row_id=row.id),
            decided_by=actor.id,
            decided_at=now,
            outcome_decision_id=stored.id,
            updated_at=now,
        )
    )
    kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action=APPEAL_RESOLVED,
        actor_type=kind,
        actor_id=actor_id,
        entity_type="appeal",
        entity_id=row.id,
        workflow=record.workflow,
        payload={
            "decision_id": record.id,
            "outcome": outcome.value,
            "outcome_decision_id": stored.id,
            "within_sla": now <= row.sla_due_at,
        },
    )
    refreshed = await session.scalar(
        select(Appeal).where(Appeal.id == appeal_id).execution_options(populate_existing=True)
    )
    assert refreshed is not None  # noqa: S101
    return AppealView.of(refreshed, record.workflow)


class AppealNotifier(Protocol):
    async def __call__(self, session: AsyncSession, appeal: Appeal) -> int: ...


async def escalate_overdue_appeals(
    session: AsyncSession, *, limit: int = 200, notifier: AppealNotifier | None = None
) -> list[uuid.UUID]:
    """Mark overdue open appeals of the session's organisation escalated (once each).

    ``notifier`` (the worker passes ``escalation.AppealEscalationNotifier``) tells the
    people who may resolve each one, in the same transaction.
    """
    now = await _now(session)
    rows = (
        await session.scalars(
            select(Appeal)
            .where(
                Appeal.status == AppealStatus.OPEN.value,
                Appeal.escalated_at.is_(None),
                Appeal.sla_due_at < now,
            )
            .order_by(Appeal.sla_due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    escalated: list[uuid.UUID] = []
    for row in rows:
        row.escalated_at = now
        row.updated_at = now
        await session.flush()
        await append_audit(
            session,
            action=APPEAL_ESCALATED,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR_ID,
            entity_type="appeal",
            entity_id=row.id,
            payload={
                "decision_id": row.decision_id,
                "overdue_seconds": int((now - row.sla_due_at).total_seconds()),
            },
        )
        if notifier is not None:
            await notifier(session, row)
        escalated.append(row.id)
    return escalated

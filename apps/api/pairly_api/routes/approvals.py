# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Unified approval inbox, decisions, delegations, approval rules and workflow runs.

* ``GET  /v1/approvals``                   inbox (only workflows where the actor holds
                                             the approval type's decision permission; an
                                             API key with ``core.approval.read`` reads
                                             its creator's inbox, never deciding)
* ``GET  /v1/approvals/{id}``              one approval: context, payload, chain, history
                                             (as the workflow's view filter shows them,
                                             ``pairly_core.approvals.views``; the hash is
                                             always the stored payload's)
* ``POST /v1/approvals/{id}/decision``     approve | reject | request_changes
                                             (``Idempotency-Key`` header required)
* ``GET|POST /v1/approval-delegations``    the caller's delegations and out-of-office
* ``DELETE /v1/approval-delegations/{id}`` revoke one
* ``GET|POST /v1/approval-rules/{type}``   rules versions (policy as code)
* ``GET  /v1/workflow-runs/{id}``          one run

All authorization happens in ``pairly_core.approvals.service``. After a decision that
ends an approval, the run is resumed in the background; the worker's reconciliation job
resumes it if this process stops first.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalStatus,
    ApprovalStep,
    Decision,
    DelegationKind,
)
from pairly_core.approvals.rules import RULES_KIND, rules_validator
from pairly_core.approvals.service import (
    DecisionOutcome,
    SlaState,
    create_delegation,
    decide,
    get_detail,
    inbox_viewer,
    list_delegations,
    list_inbox,
    revoke_delegation,
)
from pairly_core.approvals.views import approval_view
from pairly_core.audit import actor_ref
from pairly_core.errors import BadRequestError, NotFoundError, ServiceUnavailableError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.policy_versions import (
    PolicyError,
    PublishedPolicy,
    list_policy_versions,
    publish_policy_version,
)
from pairly_core.rbac import Actor, authorize
from pairly_core.workflow.runtime import RunView, WorkflowRuntime

log = structlog.get_logger(__name__)

router = APIRouter(tags=["approvals"])

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
WorkflowId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


def get_runtime(request: Request) -> WorkflowRuntime:
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if not isinstance(runtime, WorkflowRuntime):
        raise ServiceUnavailableError("workflow runtime not initialised")
    return runtime


Runtime = Annotated[WorkflowRuntime, Depends(get_runtime)]


async def resume_quietly(runtime: WorkflowRuntime, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Background resume after a decision. Failures are retried by the worker."""
    try:
        await runtime.resume_run(org_id, run_id)
    except Exception as exc:
        log.warning("workflow_resume_deferred", run_id=str(run_id), error=type(exc).__name__)


def schedule_resume(
    background: BackgroundTasks, runtime: WorkflowRuntime, actor: Actor, outcome: DecisionOutcome
) -> None:
    if outcome.approval_decided and not outcome.replayed:
        background.add_task(resume_quietly, runtime, actor.org_id, outcome.run_id)


# ------------------------------------------------------------------ models


class StepOut(BaseModel):
    id: uuid.UUID
    position: int
    name: str
    mode: str
    status: str
    assignee_ids: list[uuid.UUID]
    escalation_ids: list[uuid.UUID]
    activated_at: datetime | None
    due_at: datetime | None
    escalated_at: datetime | None
    completed_at: datetime | None


class ApprovalOut(BaseModel):
    id: uuid.UUID
    workflow: str
    approval_type: str
    title: str
    status: str
    run_id: uuid.UUID
    entity_type: str | None
    entity_id: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    sla_state: SlaState | None
    current_step: StepOut | None
    # How many guardrail findings the context names (``guardrail_findings[]``, the inbox
    # convention), so the list can say "needs a person: guardrail flag". A count only:
    # the findings themselves are in the detail.
    guardrail_findings: int = 0


class InboxOut(BaseModel):
    items: list[ApprovalOut]
    next_cursor: str | None


class DecisionRowOut(BaseModel):
    id: uuid.UUID
    step_id: uuid.UUID
    actor_user_id: uuid.UUID
    on_behalf_of: uuid.UUID | None
    via_escalation: bool
    decision: str
    reason: str | None
    channel: str
    created_at: datetime


class ApprovalDetailOut(ApprovalOut):
    context: dict[str, Any]
    payload: dict[str, Any]
    payload_hash: str
    # What the workflow hid from the approver's view of ``payload`` and ``context`` (for
    # example a candidate's name while blind), with one sentence saying so. The hash and
    # the decision always bind the real stored payload.
    masked_fields: list[str] = []
    display_note: str | None = None
    submitted_by_type: str
    submitted_by_id: str
    policy_version_id: uuid.UUID
    policy_version: int | None
    chain_name: str
    steps: list[StepOut]
    history: list[DecisionRowOut]
    can_decide: bool
    needs_second_factor: bool


class DecisionIn(BaseModel):
    decision: Decision
    reason: Annotated[str | None, Field(default=None, max_length=2000)] = None
    # Optional guard: refuse if the approval moved on from the step the user looked at.
    step_id: uuid.UUID | None = None


class DecisionOut(BaseModel):
    decision_id: uuid.UUID
    approval_id: uuid.UUID
    decision: str
    step: str
    approval_status: str
    replayed: bool


def step_out(step: ApprovalStep) -> StepOut:
    return StepOut(
        id=step.id,
        position=step.position,
        name=step.name,
        mode=step.mode,
        status=step.status,
        assignee_ids=list(step.assignee_ids),
        escalation_ids=list(step.escalation_ids),
        activated_at=step.activated_at,
        due_at=step.due_at,
        escalated_at=step.escalated_at,
        completed_at=step.completed_at,
    )


def _summary(approval: Approval, step: ApprovalStep | None, sla: SlaState | None) -> dict[str, Any]:
    return {
        "id": approval.id,
        "workflow": approval.workflow,
        "approval_type": approval.approval_type,
        "title": approval.title,
        "status": approval.status,
        "run_id": approval.run_id,
        "entity_type": approval.entity_type,
        "entity_id": approval.entity_id,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "decided_at": approval.decided_at,
        "sla_state": sla,
        "current_step": step_out(step) if step is not None else None,
        "guardrail_findings": _finding_count(approval.context),
    }


def _finding_count(context: Any) -> int:
    findings = context.get("guardrail_findings") if isinstance(context, dict) else None
    return sum(1 for f in findings if isinstance(f, dict)) if isinstance(findings, list) else 0


def _decision_row(row: ApprovalDecision) -> DecisionRowOut:
    return DecisionRowOut(
        id=row.id,
        step_id=row.step_id,
        actor_user_id=row.actor_user_id,
        on_behalf_of=row.on_behalf_of,
        via_escalation=row.via_escalation,
        decision=row.decision,
        reason=row.reason,
        channel=row.channel,
        created_at=row.created_at,
    )


def decision_out(outcome: DecisionOutcome) -> DecisionOut:
    return DecisionOut(
        decision_id=outcome.decision_id,
        approval_id=outcome.approval_id,
        decision=outcome.decision,
        step=outcome.step_name,
        approval_status=outcome.approval_status,
        replayed=outcome.replayed,
    )


# ------------------------------------------------------------------ inbox


@router.get("/approvals", response_model=InboxOut)
async def inbox(
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    workflow: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None,
    status: ApprovalStatus | None = None,
    sla: Literal["on_track", "due_soon", "overdue"] | None = None,
    assigned_to_me: bool = False,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> InboxOut:
    # An API key with core.approval.read sees its creator's inbox, read only.
    page = await list_inbox(
        session,
        await inbox_viewer(session, ctx.registry, actor),
        workflow=workflow,
        status=status,
        sla=sla,
        assigned_to_me=assigned_to_me,
        cursor=cursor,
        limit=limit,
    )
    return InboxOut(
        items=[ApprovalOut(**_summary(i.approval, i.step, i.sla)) for i in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalDetailOut)
async def detail(
    approval_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> ApprovalDetailOut:
    found = await get_detail(session, await inbox_viewer(session, ctx.registry, actor), approval_id)
    approval = found.approval
    active = found.steps[approval.current_step] if approval.current_step is not None else None
    shown = await approval_view(session, approval)
    return ApprovalDetailOut(
        **_summary(approval, active, found.sla),
        context=shown.context,
        payload=shown.payload,
        payload_hash=approval.payload_hash,
        masked_fields=list(shown.masked),
        display_note=shown.note,
        submitted_by_type=approval.submitted_by_type,
        submitted_by_id=approval.submitted_by_id,
        policy_version_id=approval.policy_version_id,
        policy_version=found.policy_version,
        chain_name=approval.chain_name,
        steps=[step_out(s) for s in found.steps],
        history=[_decision_row(d) for d in found.decisions],
        can_decide=found.can_decide,
        needs_second_factor=found.needs_second_factor,
    )


@router.post("/approvals/{approval_id}/decision", response_model=DecisionOut)
async def decision(
    approval_id: uuid.UUID,
    body: DecisionIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
    background: BackgroundTasks,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DecisionOut:
    """Decide the active step. Retrying with the same ``Idempotency-Key`` and body returns
    the recorded decision; the same key with another body is a 409."""
    if idempotency_key is None or not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise BadRequestError("an Idempotency-Key header (8 to 128 characters) is required")
    outcome = await decide(
        session,
        actor,
        approval_id,
        decision=body.decision,
        reason=body.reason,
        idempotency_key=idempotency_key,
        expected_step_id=body.step_id,
        hooks=runtime.hooks,
    )
    schedule_resume(background, runtime, actor, outcome)
    return decision_out(outcome)


# ------------------------------------------------------------------ delegations


class DelegationIn(BaseModel):
    delegate_id: uuid.UUID
    kind: DelegationKind = DelegationKind.OUT_OF_OFFICE
    starts_at: datetime
    ends_at: datetime
    workflow: WorkflowId | None = None
    reason: Annotated[str | None, Field(default=None, max_length=500)] = None


class DelegationOut(BaseModel):
    id: uuid.UUID
    delegator_id: uuid.UUID
    delegate_id: uuid.UUID
    kind: str
    workflow: str | None
    starts_at: datetime
    ends_at: datetime
    reason: str | None
    revoked_at: datetime | None


class DelegationList(BaseModel):
    items: list[DelegationOut]


def _delegation(row: ApprovalDelegation) -> DelegationOut:
    return DelegationOut(
        id=row.id,
        delegator_id=row.delegator_id,
        delegate_id=row.delegate_id,
        kind=row.kind,
        workflow=row.workflow,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        reason=row.reason,
        revoked_at=row.revoked_at,
    )


@router.get("/approval-delegations", response_model=DelegationList)
async def delegations(actor: CurrentActor, session: OrgSession) -> DelegationList:
    return DelegationList(items=[_delegation(d) for d in await list_delegations(session, actor)])


@router.post("/approval-delegations", status_code=201, response_model=DelegationOut)
async def delegate(body: DelegationIn, actor: CurrentActor, session: OrgSession) -> DelegationOut:
    row = await create_delegation(
        session,
        actor,
        delegate_id=body.delegate_id,
        kind=body.kind,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        workflow=body.workflow,
        reason=body.reason,
    )
    return _delegation(row)


@router.delete("/approval-delegations/{delegation_id}", response_model=DelegationOut)
async def undelegate(
    delegation_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> DelegationOut:
    return _delegation(await revoke_delegation(session, actor, delegation_id))


# ------------------------------------------------------------------ rules


class RulesIn(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=65536)]


class RulesVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    content: str
    content_hash: str
    created_at: datetime


class RulesList(BaseModel):
    approval_type: str
    items: list[RulesVersionOut]


ApprovalTypeKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")]


def _rules_out(policy: PublishedPolicy) -> RulesVersionOut:
    return RulesVersionOut(
        id=policy.id,
        version=policy.version,
        content=policy.content,
        content_hash=policy.content_hash,
        created_at=policy.created_at,
    )


@router.get("/approval-rules/{approval_type}", response_model=RulesList)
async def rules_versions(
    approval_type: str, actor: CurrentActor, session: OrgSession, runtime: Runtime
) -> RulesList:
    kind = runtime.approval_type(approval_type)
    if kind is None or not (actor.has(kind.permission) or actor.has("core.org.manage")):
        raise NotFoundError("approval type not found")
    versions = await list_policy_versions(session, RULES_KIND, kind.key)
    return RulesList(approval_type=kind.key, items=[_rules_out(p) for p in versions])


@router.post("/approval-rules/{approval_type}", status_code=201, response_model=RulesVersionOut)
async def publish_rules(
    approval_type: str, body: RulesIn, actor: CurrentActor, session: OrgSession, runtime: Runtime
) -> RulesVersionOut:
    """Publish a new rules version (``core.org.manage``, step-up). Validated first; old
    versions stay, and approvals keep pointing at the version they used."""
    authorize(actor, "core.org.manage")
    kind = runtime.approval_type(approval_type)
    if kind is None:
        raise NotFoundError("approval type not found")
    kind_type, actor_id = actor_ref(actor)
    try:
        published = await publish_policy_version(
            session,
            kind=RULES_KIND,
            name=kind.key,
            content=body.content,
            validate=rules_validator(kind.key),
            actor_type=kind_type,
            actor_id=actor_id,
        )
    except PolicyError as exc:
        raise BadRequestError(str(exc)) from None
    return _rules_out(published)


# ------------------------------------------------------------------ runs


class RunOut(BaseModel):
    id: uuid.UUID
    workflow: str
    graph_name: str
    graph_version: int
    status: str
    entity_type: str | None
    entity_id: str | None
    started_by_type: str
    started_by_id: str
    pending_approval_id: uuid.UUID | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


def run_out(view: RunView) -> RunOut:
    return RunOut(
        id=view.id,
        workflow=view.workflow,
        graph_name=view.graph_name,
        graph_version=view.graph_version,
        status=view.status,
        entity_type=view.entity_type,
        entity_id=view.entity_id,
        started_by_type=view.started_by_type,
        started_by_id=view.started_by_id,
        pending_approval_id=view.pending_approval_id,
        error=view.error,
        created_at=view.created_at,
        updated_at=view.updated_at,
        completed_at=view.completed_at,
    )


@router.get("/workflow-runs/{run_id}", response_model=RunOut)
async def workflow_run(
    run_id: uuid.UUID, actor: CurrentActor, session: OrgSession, runtime: Runtime
) -> RunOut:
    return run_out(await runtime.get_run(session, actor, run_id))

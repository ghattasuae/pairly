# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Approval requests, decisions, delegation and the unified inbox.

Who may decide (``decide``), checked in this order, every time, server side:

1. The actor is a person (``ActorKind.USER``). Agents can propose, never decide.
2. ``authorize(actor, approval.permission, approval)``: the actor holds the approval
   type's workflow permission (``pay.invoice.approve``) in this organisation. Because
   the permission's action is ``approve``, this also enforces the TOTP step-up rule
   (the sign-in must carry ``amr`` ``otp``). A workflow-scoped membership only counts
   for its own workflow.
3. Segregation of duties: the submitter and every preparer of the item can never decide
   it, neither directly nor as a delegate. Nobody decides two steps of the same chain,
   whether for themselves or on behalf of someone else.
4. Assignment: the actor is an approver of the active step, an escalation approver of
   it, or the delegate of an approver of it (delegation or out-of-office covering now
   and the workflow).
5. One decision per person per step (database constraint as well).

Concurrency: every decision locks the approval row (``SELECT ... FOR UPDATE``) first, so
decisions on one approval are serialised. Idempotency: a decision carries the client's
``Idempotency-Key``; the same key with the same request returns the stored decision,
the same key with a different request is a conflict. Two concurrent requests with one
key: the second waits for the lock, then finds the first decision and replays it.

Every change writes an audit event in the same transaction and calls the hooks in
``pairly_core.approvals.hooks``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import (
    NO_HOOKS,
    ApprovalDecided,
    ApprovalHooks,
    StepActivated,
)
from pairly_core.approvals.models import (
    DECIDED_STATUSES,
    Approval,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalLink,
    ApprovalStatus,
    ApprovalStep,
    Channel,
    Decision,
    DelegationKind,
    StepMode,
    StepStatus,
)
from pairly_core.approvals.rules import RULES_KIND, ApprovalRules, parse_rules, rules_validator
from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.models import Membership, Role
from pairly_core.policy_versions import (
    PolicyError,
    PublishedPolicy,
    get_policy,
    latest_policy,
    publish_policy_version,
)
from pairly_core.rbac import (
    Actor,
    ActorKind,
    Denial,
    PermissionDeniedError,
    RoleGrant,
    RoleRegistry,
    authorize,
)
from pairly_core.webhooks.events import APPROVAL_DECIDED, publish_event
from pairly_core.workflow.spec import ApprovalType

MAX_PAYLOAD_BYTES = 32 * 1024
MAX_REASON_LENGTH = 2000
MAX_IDEMPOTENCY_KEY = 128
SYSTEM_ACTOR_ID = "pairly.approvals"

SlaState = Literal["on_track", "due_soon", "overdue"]


class Actions:
    REQUESTED = "core.approval.requested"
    DECIDED = "core.approval.decided"
    STEP_ACTIVATED = "core.approval.step_activated"
    APPROVED = "core.approval.approved"
    REJECTED = "core.approval.rejected"
    CHANGES_REQUESTED = "core.approval.changes_requested"
    CANCELLED = "core.approval.cancelled"
    ESCALATED = "core.approval.escalated"
    DELEGATION_CREATED = "core.approval.delegation_created"
    DELEGATION_REVOKED = "core.approval.delegation_revoked"
    LINK_ISSUED = "core.approval.link_issued"
    LINK_USED = "core.approval.link_used"


_STATUS_ACTION = {
    ApprovalStatus.APPROVED: Actions.APPROVED,
    ApprovalStatus.REJECTED: Actions.REJECTED,
    ApprovalStatus.CHANGES_REQUESTED: Actions.CHANGES_REQUESTED,
}


# ------------------------------------------------------------------ errors


class ApprovalNotFoundError(NotFoundError):
    code = "approval_not_found"
    title = "Approval not found"


class ApprovalStateError(ConflictError):
    code = "approval_state_conflict"
    title = "The approval cannot take this decision now"


class AlreadyDecidedError(ConflictError):
    code = "already_decided"
    title = "You already decided this step"


class IdempotencyConflictError(ConflictError):
    code = "idempotency_key_reused"
    title = "Idempotency key reused with a different request"


class SegregationOfDutiesError(PermissionDeniedError):
    code = "segregation_of_duties"
    title = "Segregation of duties"


class NotAssignedError(PermissionDeniedError):
    code = "not_assigned"
    title = "Not an approver of this step"


class NoEligibleApproverError(PolicyError):
    """A step of the chosen chain has nobody who may approve it (fail closed)."""


class InvalidPayloadError(BadRequestError):
    code = "invalid_payload"


# ------------------------------------------------------------------ payloads


def _check_json(value: object, depth: int = 0, *, allow_floats: bool = False) -> None:
    if depth > 16:
        raise InvalidPayloadError("payload nested too deep")
    if value is None or isinstance(value, bool | int | str):
        if isinstance(value, str) and "\x00" in value:
            raise InvalidPayloadError("payload strings cannot contain NUL")
        return
    if isinstance(value, float):
        if allow_floats and math.isfinite(value):
            return
        raise InvalidPayloadError("payload floats are not allowed: use strings for amounts")
    if isinstance(value, list | tuple):
        for item in value:
            _check_json(item, depth + 1, allow_floats=allow_floats)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidPayloadError("payload keys must be strings")
            _check_json(item, depth + 1, allow_floats=allow_floats)
        return
    raise InvalidPayloadError(f"unsupported payload type {type(value).__name__}")


def canonical_payload(
    payload: Mapping[str, Any], *, allow_floats: bool = False
) -> tuple[dict[str, Any], str]:
    """A JSON copy of ``payload`` and its SHA-256 over canonical JSON (sorted keys).

    The executor recomputes this hash from the run state before any final action, so a
    payload never holds floats (amounts are strings). An approval's ``context`` is shown,
    never executed or hashed for execution: it may hold finite floats (page coordinates
    of evidence boxes, ``allow_floats=True``).
    """
    if not isinstance(payload, Mapping):
        raise InvalidPayloadError("payload must be a JSON object")
    _check_json(payload, allow_floats=allow_floats)
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise InvalidPayloadError("payload too large")
    data: dict[str, Any] = json.loads(raw)
    return data, hashlib.sha256(raw).hexdigest()


def payload_hash(payload: Mapping[str, Any]) -> str:
    return canonical_payload(payload)[1]


# ------------------------------------------------------------------ helpers


async def db_now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.now()))
    if not isinstance(now, datetime):  # pragma: no cover - the database always answers
        raise RuntimeError("database returned no time")
    return now


def _uuid_list(values: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    return sorted(set(values), key=str)


async def member_permissions(
    session: AsyncSession, registry: RoleRegistry, user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    """Effective permissions of each user in the session's organisation (members only)."""
    ids = list(set(user_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Membership.user_id, Role.key, Membership.workflow)
            .join(Role, Role.id == Membership.role_id)
            .where(Membership.user_id.in_(ids))
        )
    ).all()
    grants: dict[uuid.UUID, set[RoleGrant]] = {}
    for row in rows:
        grants.setdefault(row.user_id, set()).add(RoleGrant(row.key, row.workflow))
    return {user: registry.resolve(g) for user, g in grants.items()}


async def role_members(
    session: AsyncSession, roles: Sequence[str], workflow: str
) -> set[uuid.UUID]:
    if not roles:
        return set()
    rows = await session.scalars(
        select(Membership.user_id)
        .join(Role, Role.id == Membership.role_id)
        .where(
            Role.key.in_(list(roles)),
            or_(Membership.workflow.is_(None), Membership.workflow == workflow),
        )
    )
    return set(rows)


async def active_delegations(
    session: AsyncSession,
    *,
    workflow: str,
    now: datetime,
    delegate_id: uuid.UUID | None = None,
    delegator_ids: Iterable[uuid.UUID] | None = None,
) -> list[ApprovalDelegation]:
    query = select(ApprovalDelegation).where(
        ApprovalDelegation.revoked_at.is_(None),
        ApprovalDelegation.starts_at <= now,
        ApprovalDelegation.ends_at > now,
        or_(ApprovalDelegation.workflow.is_(None), ApprovalDelegation.workflow == workflow),
    )
    if delegate_id is not None:
        query = query.where(ApprovalDelegation.delegate_id == delegate_id)
    if delegator_ids is not None:
        query = query.where(ApprovalDelegation.delegator_id.in_(list(delegator_ids)))
    return list(await session.scalars(query.order_by(ApprovalDelegation.created_at)))


def excluded_users(approval: Approval) -> frozenset[uuid.UUID]:
    """People who may never decide this approval: the submitter and every preparer."""
    people = set(approval.preparer_ids or ())
    if approval.submitted_by_type == ActorType.USER.value:
        # Always a UUID (written by this module); a bad value would raise, failing closed.
        people.add(uuid.UUID(approval.submitted_by_id))
    return frozenset(people)


def sla_state(step: ApprovalStep | None, now: datetime) -> SlaState | None:
    if step is None or step.status != StepStatus.ACTIVE or step.due_at is None:
        return None
    if now > step.due_at:
        return "overdue"
    if step.warn_at is not None and now >= step.warn_at:
        return "due_soon"
    return "on_track"


def _activate(step: ApprovalStep, now: datetime) -> None:
    step.status = StepStatus.ACTIVE
    step.activated_at = now
    step.due_at = now + timedelta(seconds=step.sla_seconds)
    step.warn_at = step.due_at - timedelta(seconds=step.warn_seconds)


def _deny(
    error: type[PermissionDeniedError], actor: Actor, approval: Approval, reason: str, detail: str
) -> PermissionDeniedError:
    return error(
        detail,
        denial=Denial(
            actor=actor,
            permission=approval.permission,
            reason=reason,
            resource_type="Approval",
            resource_id=str(approval.id),
        ),
    )


# ------------------------------------------------------------------ rules


async def rules_for(
    session: AsyncSession, approval_type: ApprovalType
) -> tuple[PublishedPolicy, ApprovalRules]:
    """The latest rules version for the type; publishes the plugin default as version 1."""
    policy = await latest_policy(session, RULES_KIND, approval_type.key)
    if policy is None:
        policy = await publish_policy_version(
            session,
            kind=RULES_KIND,
            name=approval_type.key,
            content=approval_type.default_rules,
            validate=rules_validator(approval_type.key),
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR_ID,
        )
    rules = parse_rules(policy.content)
    if rules.approval_type != approval_type.key:
        raise PolicyError("stored approval rules are for another approval type")
    return policy, rules


async def rules_of(session: AsyncSession, approval: Approval) -> ApprovalRules:
    """The exact rules version an approval was created under."""
    policy = await get_policy(session, approval.policy_version_id)
    if policy is None:
        raise PolicyError("the approval's rules version is missing")
    return parse_rules(policy.content)


# ------------------------------------------------------------------ requesting


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    run_id: uuid.UUID
    step_key: str
    approval_type: ApprovalType
    title: str
    payload: Mapping[str, Any]
    context: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1
    entity_type: str | None = None
    entity_id: str | None = None
    submitted_by_type: ActorType = ActorType.SYSTEM
    submitted_by_id: str = SYSTEM_ACTOR_ID
    preparer_ids: Sequence[uuid.UUID] = ()


async def create_approval(
    session: AsyncSession,
    registry: RoleRegistry,
    request: ApprovalRequest,
    *,
    hooks: ApprovalHooks = NO_HOOKS,
) -> Approval:
    """Create the approval and its chain, or return the one already created for this run,
    step and revision (graph nodes may run again after a crash)."""
    existing = await session.scalar(
        select(Approval).where(
            Approval.run_id == request.run_id,
            Approval.step_key == request.step_key,
            Approval.revision == request.revision,
        )
    )
    if existing is not None:
        return existing

    approval_type = request.approval_type
    payload, digest = canonical_payload(request.payload)
    context, _ = canonical_payload(request.context, allow_floats=True)
    policy, rules = await rules_for(session, approval_type)
    chain = rules.select_chain(payload)
    now = await db_now(session)

    approval = Approval(
        id=uuid.uuid4(),
        workflow=approval_type.workflow,
        approval_type=approval_type.key,
        permission=approval_type.permission,
        run_id=request.run_id,
        step_key=request.step_key,
        revision=request.revision,
        entity_type=request.entity_type,
        entity_id=request.entity_id,
        title=request.title[:200],
        context=context,
        payload=payload,
        payload_hash=digest,
        status=ApprovalStatus.PENDING,
        submitted_by_type=request.submitted_by_type.value,
        submitted_by_id=request.submitted_by_id,
        preparer_ids=_uuid_list(request.preparer_ids),
        policy_version_id=policy.id,
        chain_name=chain.name,
        current_step=0,
        state_version=1,
        created_at=now,
        updated_at=now,
    )
    excluded = excluded_users(approval)

    steps: list[ApprovalStep] = []
    for position, rule in enumerate(chain.steps):
        candidates = await role_members(session, rule.roles, approval_type.workflow)
        candidates |= set(rule.users)
        perms = await member_permissions(session, registry, candidates)
        eligible = {
            user
            for user in candidates
            if approval_type.permission in perms.get(user, frozenset()) and user not in excluded
        }
        if not eligible:
            raise NoEligibleApproverError(f"nobody may approve step {rule.name!r}")
        sla = rules.step_sla_seconds(rule)
        step = ApprovalStep(
            id=uuid.uuid4(),
            approval_id=approval.id,
            position=position,
            name=rule.name,
            mode=rule.mode,
            status=StepStatus.WAITING,
            assignee_ids=_uuid_list(eligible),
            escalation_ids=[],
            sla_seconds=sla,
            warn_seconds=rules.warn_seconds(sla),
            escalation_level=0,
        )
        steps.append(step)
    _activate(steps[0], now)

    session.add(approval)
    await session.flush()
    session.add_all(steps)
    await session.flush()

    await append_audit(
        session,
        action=Actions.REQUESTED,
        actor_type=request.submitted_by_type,
        actor_id=request.submitted_by_id,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={
            "run_id": str(request.run_id),
            "approval_type": approval.approval_type,
            "step_key": approval.step_key,
            "revision": approval.revision,
            "policy_version_id": str(policy.id),
            "policy_version": policy.version,
            "chain": chain.name,
            "steps": len(steps),
            "payload_hash": digest,
        },
    )
    await _step_activated(session, approval, steps[0], hooks)
    return approval


async def _step_activated(
    session: AsyncSession, approval: Approval, step: ApprovalStep, hooks: ApprovalHooks
) -> None:
    assert step.due_at is not None  # noqa: S101 (set by _activate)
    await append_audit(
        session,
        action=Actions.STEP_ACTIVATED,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR_ID,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={
            "step": step.name,
            "position": step.position,
            "assignees": [str(u) for u in step.assignee_ids],
            "due_at": step.due_at.isoformat(),
        },
    )
    await hooks.step_activated(
        session,
        StepActivated(
            org_id=approval.org_id,
            approval_id=approval.id,
            step_id=step.id,
            step_name=step.name,
            assignee_ids=tuple(step.assignee_ids),
            due_at=step.due_at,
        ),
    )


# ------------------------------------------------------------------ deciding


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    decision_id: uuid.UUID
    approval_id: uuid.UUID
    run_id: uuid.UUID
    decision: str
    approval_status: str
    step_name: str
    replayed: bool

    @property
    def approval_decided(self) -> bool:
        return self.approval_status in DECIDED_STATUSES


def request_hash(approval_id: uuid.UUID, decision: str, reason: str | None) -> str:
    raw = json.dumps([str(approval_id), decision, reason or ""], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_reason(decision: Decision, reason: str | None) -> str | None:
    text_ = (reason or "").strip()
    if len(text_) > MAX_REASON_LENGTH:
        raise BadRequestError(f"reason longer than {MAX_REASON_LENGTH} characters")
    if "\x00" in text_:
        raise BadRequestError("invalid reason")
    if decision is not Decision.APPROVE and not text_:
        raise BadRequestError("a reason is required to reject or request changes")
    return text_ or None


async def _steps(session: AsyncSession, approval_id: uuid.UUID) -> list[ApprovalStep]:
    return list(
        await session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.approval_id == approval_id)
            .order_by(ApprovalStep.position)
        )
    )


async def _decisions(session: AsyncSession, approval_id: uuid.UUID) -> list[ApprovalDecision]:
    return list(
        await session.scalars(
            select(ApprovalDecision)
            .where(ApprovalDecision.approval_id == approval_id)
            .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
        )
    )


def _outcome(
    approval: Approval, row: ApprovalDecision, step: str, replayed: bool
) -> DecisionOutcome:
    return DecisionOutcome(
        decision_id=row.id,
        approval_id=approval.id,
        run_id=approval.run_id,
        decision=row.decision,
        approval_status=approval.status,
        step_name=step,
        replayed=replayed,
    )


@dataclass(frozen=True, slots=True)
class _Seat:
    principal: uuid.UUID | None  # the approver decided for; None for escalation
    on_behalf_of: uuid.UUID | None
    via_escalation: bool


async def _seat_for(
    session: AsyncSession,
    actor: Actor,
    approval: Approval,
    step: ApprovalStep,
    decided_principals: set[uuid.UUID],
    now: datetime,
) -> _Seat | None:
    """How ``actor`` may decide ``step``: as an approver, a delegate, or by escalation."""
    if actor.id in step.assignee_ids:
        return _Seat(principal=actor.id, on_behalf_of=None, via_escalation=False)
    delegations = await active_delegations(
        session,
        workflow=approval.workflow,
        now=now,
        delegate_id=actor.id,
        delegator_ids=step.assignee_ids,
    )
    open_principals = [
        d.delegator_id for d in delegations if d.delegator_id not in decided_principals
    ]
    if open_principals:
        # A delegation lends a seat, never more than its holder has: a delegator who
        # left the organisation has no seat to lend (the executor also re-checks that
        # every principal still holds the permission; security review 2026-10, M-01).
        members = set(
            await session.scalars(
                select(Membership.user_id).where(Membership.user_id.in_(open_principals))
            )
        )
        open_principals = [p for p in open_principals if p in members]
    if open_principals:
        principal = open_principals[0]
        return _Seat(principal=principal, on_behalf_of=principal, via_escalation=False)
    if actor.id in step.escalation_ids:
        return _Seat(principal=None, on_behalf_of=None, via_escalation=True)
    return None


def step_complete(step: ApprovalStep, decisions: Sequence[ApprovalDecision]) -> bool:
    """Whether the approving decisions on ``step`` satisfy its mode.

    ``decisions`` are all decisions of the approval. For ``all_of``, approvers who
    decided another step of the chain are not required (segregation of duties forbids
    them to decide this one); if that leaves nobody, only an escalation approver can
    complete the step.
    """
    approvals = [
        d for d in decisions if d.step_id == step.id and d.decision == Decision.APPROVE.value
    ]
    if not approvals:
        return False
    if step.mode == StepMode.ANY_OF or any(d.via_escalation for d in approvals):
        return True
    blocked = {
        person
        for d in decisions
        if d.step_id != step.id
        for person in (d.actor_user_id, d.on_behalf_of)
        if person is not None
    }
    required = set(step.assignee_ids) - blocked
    covered = {d.on_behalf_of or d.actor_user_id for d in approvals}
    return bool(required) and required <= covered


async def decide(
    session: AsyncSession,
    actor: Actor,
    approval_id: uuid.UUID,
    *,
    decision: Decision,
    reason: str | None,
    idempotency_key: str | None,
    channel: Channel = Channel.WEB,
    expected_step_id: uuid.UUID | None = None,
    link_id: uuid.UUID | None = None,
    hooks: ApprovalHooks = NO_HOOKS,
) -> DecisionOutcome:
    """Record ``actor``'s decision on the active step. See the module docstring."""
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY:
        raise BadRequestError("invalid idempotency key")
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None:
        raise ApprovalNotFoundError()
    if actor.kind is not ActorKind.USER:
        raise _deny(
            PermissionDeniedError, actor, approval, "agent_cannot_decide", "agents cannot decide"
        )
    authorize(actor, approval.permission, approval)

    clean_reason = _clean_reason(decision, reason)
    digest = request_hash(approval.id, decision.value, clean_reason)
    if idempotency_key is not None:
        previous = await session.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.actor_user_id == actor.id,
                ApprovalDecision.idempotency_key == idempotency_key,
            )
        )
        if previous is not None:
            if previous.request_hash != digest or previous.approval_id != approval.id:
                raise IdempotencyConflictError()
            step_name = await session.scalar(
                select(ApprovalStep.name).where(ApprovalStep.id == previous.step_id)
            )
            return _outcome(approval, previous, step_name or "", replayed=True)

    if approval.status != ApprovalStatus.PENDING or approval.current_step is None:
        raise ApprovalStateError("the approval is no longer pending")
    steps = await _steps(session, approval.id)
    step = steps[approval.current_step]
    if step.status != StepStatus.ACTIVE:  # pragma: no cover - invariant
        raise ApprovalStateError("the approval has no active step")
    if expected_step_id is not None and expected_step_id != step.id:
        raise ApprovalStateError("the approval moved to another step")

    excluded = excluded_users(approval)
    if actor.id in excluded:
        raise _deny(
            SegregationOfDutiesError,
            actor,
            approval,
            "segregation_of_duties",
            "the submitter or a preparer cannot decide this item",
        )
    decisions = await _decisions(session, approval.id)
    on_this_step = [d for d in decisions if d.step_id == step.id]
    if any(d.actor_user_id == actor.id for d in on_this_step):
        raise AlreadyDecidedError()
    decided_principals = {d.on_behalf_of or d.actor_user_id for d in on_this_step}
    now = await db_now(session)
    seat = await _seat_for(session, actor, approval, step, decided_principals, now)
    if seat is None:
        raise _deny(
            NotAssignedError,
            actor,
            approval,
            "not_assigned",
            "you are not an approver of this step",
        )
    if seat.principal is not None and seat.principal in decided_principals:
        raise AlreadyDecidedError("this approver's decision is already recorded")
    if seat.principal is not None and seat.principal in excluded:  # pragma: no cover
        raise _deny(
            SegregationOfDutiesError, actor, approval, "segregation_of_duties", "not allowed"
        )
    people = {actor.id} | ({seat.principal} if seat.principal else set())
    for other in decisions:
        if other.step_id != step.id and people & {other.actor_user_id, other.on_behalf_of}:
            raise _deny(
                SegregationOfDutiesError,
                actor,
                approval,
                "segregation_of_duties",
                "one person cannot decide two steps of the same approval",
            )

    row = ApprovalDecision(
        id=uuid.uuid4(),
        approval_id=approval.id,
        step_id=step.id,
        actor_user_id=actor.id,
        on_behalf_of=seat.on_behalf_of,
        via_escalation=seat.via_escalation,
        decision=decision.value,
        reason=clean_reason,
        channel=channel.value,
        link_id=link_id,
        permission=approval.permission,
        second_factor=actor.has_second_factor,
        idempotency_key=idempotency_key,
        request_hash=digest,
        created_at=now,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # Only reachable when the same key is used concurrently on another approval
        # (decisions on one approval are serialised by the row lock).
        raise IdempotencyConflictError() from None
    decisions.append(row)

    previous_version = approval.state_version
    activated: ApprovalStep | None = None
    if decision is Decision.APPROVE:
        if step_complete(step, decisions):
            step.status = StepStatus.APPROVED
            step.completed_at = now
            following = [s for s in steps if s.position > step.position]
            if following:
                activated = following[0]
                _activate(activated, now)
                approval.current_step = activated.position
            else:
                _finish(approval, ApprovalStatus.APPROVED, now)
            approval.state_version += 1
    else:
        terminal = (
            ApprovalStatus.REJECTED
            if decision is Decision.REJECT
            else ApprovalStatus.CHANGES_REQUESTED
        )
        step.status = (
            StepStatus.REJECTED if decision is Decision.REJECT else StepStatus.CHANGES_REQUESTED
        )
        step.completed_at = now
        for later in steps:
            if later.position > step.position:
                later.status = StepStatus.CANCELLED
        _finish(approval, terminal, now)
        approval.state_version += 1
    approval.updated_at = now
    await session.flush()

    await invalidate_links(
        session,
        approval.id,
        now,
        all_open=approval.state_version != previous_version,
        step_id=step.id,
        user_id=actor.id,
        keep=link_id,
    )
    await audit_actor_action(
        session,
        actor,
        Actions.DECIDED,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={
            "run_id": str(approval.run_id),
            "decision_id": str(row.id),
            "decision": decision.value,
            "step": step.name,
            "on_behalf_of": str(seat.on_behalf_of) if seat.on_behalf_of else None,
            "via_escalation": seat.via_escalation,
            "channel": channel.value,
            "approval_status": approval.status,
            "policy_version_id": str(approval.policy_version_id),
        },
    )
    if approval.status in _STATUS_ACTION:
        await append_audit(
            session,
            action=_STATUS_ACTION[ApprovalStatus(approval.status)],
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR_ID,
            entity_type="approval",
            entity_id=approval.id,
            workflow=approval.workflow,
            payload={"run_id": str(approval.run_id), "payload_hash": approval.payload_hash},
        )
    if activated is not None:
        await _step_activated(session, approval, activated, hooks)
    # Outbound webhook (transactional outbox): emitted only if this decision commits.
    await publish_event(
        session,
        APPROVAL_DECIDED,
        {
            "approval_id": str(approval.id),
            "run_id": str(approval.run_id),
            "workflow": approval.workflow,
            "approval_type": approval.approval_type,
            "step": step.name,
            "decision_id": str(row.id),
            "decision": decision.value,
            "approval_status": approval.status,
            "entity_type": approval.entity_type,
            "entity_id": approval.entity_id,
        },
    )
    await hooks.approval_decided(
        session,
        ApprovalDecided(
            org_id=approval.org_id,
            approval_id=approval.id,
            run_id=approval.run_id,
            workflow=approval.workflow,
            approval_type=approval.approval_type,
            policy_version_id=approval.policy_version_id,
            payload_hash=approval.payload_hash,
            step_id=step.id,
            step_name=step.name,
            decision_id=row.id,
            decision=decision.value,
            reason=clean_reason,
            actor_user_id=actor.id,
            on_behalf_of=seat.on_behalf_of,
            channel=channel.value,
            approval_status=approval.status,
            decided_at=now,
        ),
    )
    return _outcome(approval, row, step.name, replayed=False)


def _finish(approval: Approval, status: ApprovalStatus, now: datetime) -> None:
    approval.status = status
    approval.decided_at = now
    approval.current_step = None


async def invalidate_links(
    session: AsyncSession,
    approval_id: uuid.UUID,
    now: datetime,
    *,
    all_open: bool,
    step_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    keep: uuid.UUID | None = None,
) -> None:
    """Invalidate signed links of an approval: every open link when its state changed,
    else the deciding user's links for the step."""
    conditions = [
        ApprovalLink.approval_id == approval_id,
        ApprovalLink.used_at.is_(None),
        ApprovalLink.invalidated_at.is_(None),
    ]
    if keep is not None:
        conditions.append(ApprovalLink.id != keep)
    if not all_open:
        conditions += [ApprovalLink.step_id == step_id, ApprovalLink.user_id == user_id]
    await session.execute(
        update(ApprovalLink)
        .where(and_(*conditions))
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )


async def cancel_approval(
    session: AsyncSession, approval_id: uuid.UUID, *, actor_type: ActorType, actor_id: str
) -> None:
    """Cancel a pending approval (its run was cancelled). Idempotent."""
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    now = await db_now(session)
    for step in await _steps(session, approval.id):
        if step.status in {StepStatus.ACTIVE, StepStatus.WAITING}:
            step.status = StepStatus.CANCELLED
    approval.status = ApprovalStatus.CANCELLED
    approval.current_step = None
    approval.state_version += 1
    approval.updated_at = now
    await session.flush()
    await invalidate_links(session, approval.id, now, all_open=True)
    await append_audit(
        session,
        action=Actions.CANCELLED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={"run_id": str(approval.run_id)},
    )


# ------------------------------------------------------------------ reading


@dataclass(frozen=True, slots=True)
class InboxItem:
    approval: Approval
    step: ApprovalStep | None
    sla: SlaState | None


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: list[InboxItem]
    next_cursor: str | None


def _encode_cursor(created_at: datetime, approval_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(approval_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, ident = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(created), uuid.UUID(ident)
    except (ValueError, TypeError):
        raise BadRequestError("invalid cursor") from None


def decider_permissions(actor: Actor) -> list[str]:
    """The approve permissions the actor holds: the inbox shows only those workflows."""
    return sorted(p for p in actor.permissions if p.rsplit(".", 1)[1] == "approve")


INBOX_READ = "core.approval.read"


async def inbox_viewer(session: AsyncSession, registry: RoleRegistry, actor: Actor) -> Actor:
    """Whose inbox ``actor`` reads (``GET /v1/approvals`` and ``/v1/approvals/{id}``).

    A person or the agent reads their own. An API key holding ``core.approval.read``
    reads the inbox of the person who created it (a personal key, for example for the
    MCP server: "which approvals are waiting for me?"), read only:

    * the permissions are the creator's approve permissions as of now (a creator who
      left or lost the role sees nothing through the key any more);
    * the returned actor stays an ``api_key`` actor, so ``authorize`` refuses it every
      approve or decide permission, ``decide`` refuses it outright and ``can_decide`` is
      always false. It is only ever used to filter what is shown.
    """
    if actor.kind is not ActorKind.API_KEY:
        return actor
    authorize(actor, INBOX_READ)
    from pairly_core.api_keys.models import ApiKey
    from pairly_core.models import User

    creator = await session.scalar(select(ApiKey.created_by).where(ApiKey.id == actor.id))
    if creator is None:
        raise PermissionDeniedError("this API key has no owner")
    user = await session.scalar(select(User).where(User.id == creator))
    rows = (
        await session.execute(
            select(Role.key, Membership.workflow)
            .join(Role, Role.id == Membership.role_id)
            .where(Membership.user_id == creator, Membership.org_id == actor.org_id)
        )
    ).all()
    grants = tuple(RoleGrant(role=r.key, workflow=r.workflow) for r in rows)
    held = registry.resolve(grants) if user is not None else frozenset()
    return Actor(
        kind=ActorKind.API_KEY,
        id=creator,
        org_id=actor.org_id,
        grants=(),
        permissions=frozenset(p for p in held if p.rsplit(".", 1)[1] == "approve"),
    )


async def list_inbox(
    session: AsyncSession,
    actor: Actor,
    *,
    workflow: str | None = None,
    status: ApprovalStatus | None = None,
    sla: SlaState | None = None,
    assigned_to_me: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> InboxPage:
    """Approvals of the workflows where ``actor`` holds the decision permission."""
    permissions = decider_permissions(actor)
    if not permissions:
        return InboxPage(items=[], next_cursor=None)
    now = await db_now(session)
    active = and_(
        ApprovalStep.approval_id == Approval.id, ApprovalStep.position == Approval.current_step
    )
    query = (
        select(Approval, ApprovalStep)
        .outerjoin(ApprovalStep, active)
        .where(Approval.permission.in_(permissions))
    )
    if workflow is not None:
        query = query.where(Approval.workflow == workflow)
    if status is not None:
        query = query.where(Approval.status == status.value)
    if sla is not None:
        query = query.where(Approval.status == ApprovalStatus.PENDING.value)
        if sla == "overdue":
            query = query.where(ApprovalStep.due_at < now)
        elif sla == "due_soon":
            query = query.where(ApprovalStep.warn_at <= now, ApprovalStep.due_at >= now)
        else:
            query = query.where(ApprovalStep.warn_at > now)
    if assigned_to_me:
        mine = [
            ApprovalStep.assignee_ids.contains([actor.id]),
            ApprovalStep.escalation_ids.contains([actor.id]),
        ]
        delegations = list(
            await session.scalars(
                select(ApprovalDelegation).where(
                    ApprovalDelegation.delegate_id == actor.id,
                    ApprovalDelegation.revoked_at.is_(None),
                    ApprovalDelegation.starts_at <= now,
                    ApprovalDelegation.ends_at > now,
                )
            )
        )
        for d in delegations:
            clause = ApprovalStep.assignee_ids.contains([d.delegator_id])
            if d.workflow is not None:
                clause = and_(clause, Approval.workflow == d.workflow)
            mine.append(clause)
        excluded = or_(
            Approval.preparer_ids.contains([actor.id]),
            and_(
                Approval.submitted_by_type == ActorType.USER.value,
                Approval.submitted_by_id == str(actor.id),
            ),
        )
        query = query.where(Approval.status == ApprovalStatus.PENDING.value, or_(*mine), ~excluded)
    if cursor is not None:
        created, ident = _decode_cursor(cursor)
        query = query.where(
            or_(
                Approval.created_at < created,
                and_(Approval.created_at == created, Approval.id < ident),
            )
        )
    limit = max(1, min(limit, 200))
    rows = (
        await session.execute(
            query.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(limit + 1)
        )
    ).all()
    items = [InboxItem(approval=a, step=s, sla=sla_state(s, now)) for a, s in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1].approval
        next_cursor = _encode_cursor(last.created_at, last.id)
    return InboxPage(items=items, next_cursor=next_cursor)


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    approval: Approval
    steps: list[ApprovalStep]
    decisions: list[ApprovalDecision]
    sla: SlaState | None
    policy_version: int | None
    can_decide: bool
    needs_second_factor: bool


async def get_detail(session: AsyncSession, actor: Actor, approval_id: uuid.UUID) -> ApprovalDetail:
    """One approval with its chain and history. Hidden (404) without the permission."""
    approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
    if approval is None or not actor.has(approval.permission):
        raise ApprovalNotFoundError()
    steps = await _steps(session, approval.id)
    decisions = await _decisions(session, approval.id)
    now = await db_now(session)
    active = steps[approval.current_step] if approval.current_step is not None else None
    policy = await get_policy(session, approval.policy_version_id)
    can_decide = False
    if active is not None and actor.kind is ActorKind.USER:
        decided = {d.on_behalf_of or d.actor_user_id for d in decisions if d.step_id == active.id}
        others = {
            p
            for d in decisions
            if d.step_id != active.id
            for p in (d.actor_user_id, d.on_behalf_of)
            if p is not None
        }
        seat = await _seat_for(session, actor, approval, active, decided, now)
        can_decide = (
            seat is not None
            and actor.id not in excluded_users(approval)
            and actor.id not in others
            and not any(d.actor_user_id == actor.id for d in decisions if d.step_id == active.id)
        )
    return ApprovalDetail(
        approval=approval,
        steps=steps,
        decisions=decisions,
        sla=sla_state(active, now),
        policy_version=policy.version if policy else None,
        can_decide=can_decide,
        needs_second_factor=not actor.has_second_factor,
    )


# ------------------------------------------------------------------ delegation


async def create_delegation(
    session: AsyncSession,
    actor: Actor,
    *,
    delegate_id: uuid.UUID,
    kind: DelegationKind,
    starts_at: datetime,
    ends_at: datetime,
    workflow: str | None = None,
    reason: str | None = None,
) -> ApprovalDelegation:
    """Delegate the actor's own approvals (or mark them out of office) for a period."""
    if actor.kind is not ActorKind.USER:
        raise PermissionDeniedError("only people can delegate")
    if delegate_id == actor.id:
        raise BadRequestError("you cannot delegate to yourself")
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise BadRequestError("invalid period")
    if ends_at - starts_at > timedelta(days=366):
        raise BadRequestError("a delegation can last one year at most")
    member = await session.scalar(
        select(Membership.id).where(Membership.user_id == delegate_id).limit(1)
    )
    if member is None:
        raise BadRequestError("the delegate is not a member of this organisation")
    row = ApprovalDelegation(
        id=uuid.uuid4(),
        delegator_id=actor.id,
        delegate_id=delegate_id,
        kind=kind.value,
        workflow=workflow,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=(reason or "").strip()[:500] or None,
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        Actions.DELEGATION_CREATED,
        entity_type="approval_delegation",
        entity_id=row.id,
        workflow=workflow,
        payload={
            "delegate_id": str(delegate_id),
            "kind": kind.value,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    return row


async def revoke_delegation(
    session: AsyncSession, actor: Actor, delegation_id: uuid.UUID
) -> ApprovalDelegation:
    row = await session.scalar(
        select(ApprovalDelegation).where(
            ApprovalDelegation.id == delegation_id, ApprovalDelegation.delegator_id == actor.id
        )
    )
    if row is None:
        raise NotFoundError("delegation not found")
    if row.revoked_at is None:
        row.revoked_at = await db_now(session)
        await session.flush()
        await audit_actor_action(
            session,
            actor,
            Actions.DELEGATION_REVOKED,
            entity_type="approval_delegation",
            entity_id=row.id,
            workflow=row.workflow,
        )
    return row


async def list_delegations(session: AsyncSession, actor: Actor) -> list[ApprovalDelegation]:
    return list(
        await session.scalars(
            select(ApprovalDelegation)
            .where(
                or_(
                    ApprovalDelegation.delegator_id == actor.id,
                    ApprovalDelegation.delegate_id == actor.id,
                )
            )
            .order_by(ApprovalDelegation.starts_at.desc())
            .limit(200)
        )
    )

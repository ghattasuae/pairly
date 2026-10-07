# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Signed single-use approval links for email and Slack (brief 2.2.9 and 6.7).

A link is a random 32-byte token (``secrets.token_urlsafe``, 43 characters). Only its
SHA-256 is stored, in ``approval_links``, bound to:

* one approval, one step of it, and the approval's ``state_version`` at issue time,
* one person (the user the link was sent to), and
* one action (``approve``, ``reject`` or ``request_changes``).

It expires quickly (``PAIRLY_APPROVAL_LINK_TTL_SECONDS``, default 30 minutes), works
once, and dies as soon as the approval changes state (any decision that completes a
step or ends the approval invalidates every open link of the approval; a person's own
decision invalidates their other links for that step).

Why the link does not approve by itself (documented design choice):

* ``GET /v1/approval-links/{token}`` only describes what the link would do. It never
  changes state, so mail scanners and link previews that fetch URLs cannot decide.
* ``POST /v1/approval-links/{token}`` confirms. It needs a signed-in session of the
  bound user (bearer JWT), and the decision goes through the same ``decide`` as the
  inbox: workflow permission, segregation of duties, assignment, and the TOTP step-up
  rule (the approve permission is a step-up permission, so the sign-in must carry
  ``amr`` ``otp``). The link therefore counts as the bound user's decision, made in
  a TOTP-backed session, never as an anonymous one. A stolen link is useless without
  that person's sign-in and second factor.
* The email or Slack button points at the web app's confirmation page with the token in
  the URL fragment (``/approvals/link#<token>``), which browsers never send to a
  server. The page asks the user to sign in (with TOTP) if needed, shows the GET
  answer, and POSTs on an explicit click. The API paths carry the token, so the API
  access log and traces redact that path segment.

Tokens are looked up across organisations through ``approval_link_org_id(hash)``, a
narrow SECURITY DEFINER function (migration 0006), then read under normal RLS.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalLink,
    ApprovalStatus,
    ApprovalStep,
    Channel,
    Decision,
)
from pairly_core.approvals.service import (
    SYSTEM_ACTOR_ID,
    Actions,
    ApprovalNotFoundError,
    ApprovalStateError,
    DecisionOutcome,
    active_delegations,
    db_now,
    decide,
    excluded_users,
)
from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.db import current_org
from pairly_core.errors import BadRequestError, PairlyError
from pairly_core.rbac import Actor

TOKEN_BYTES = 32
DEFAULT_TTL_SECONDS = 30 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 3600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

LinkState = Literal["valid", "used", "expired", "invalidated"]


class LinkInvalidError(PairlyError):
    """Unknown, foreign, used, expired or invalidated link. One answer for all of them."""

    status = 410
    code = "link_invalid"
    title = "This link is no longer valid"


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_well_formed_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


@dataclass(frozen=True, slots=True)
class IssuedLink:
    id: uuid.UUID
    token: str  # shown once to the sender (email or Slack); never stored or logged
    approval_id: uuid.UUID
    step_id: uuid.UUID
    user_id: uuid.UUID
    action: str
    expires_at: datetime


async def issue_link(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    user_id: uuid.UUID,
    action: Decision,
    channel: Channel,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> IssuedLink:
    """Issue a link for ``user_id`` to take ``action`` on the approval's active step.

    Refuses people who could not decide the step anyway (not assigned, submitter or
    preparer, already decided). The permission and second factor are checked when the
    link is used, against the live session.
    """
    if channel not in {Channel.EMAIL_LINK, Channel.SLACK_LINK}:
        raise BadRequestError("links are for email or Slack")
    if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
        raise BadRequestError("invalid link lifetime")
    approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
    if approval is None:
        raise ApprovalNotFoundError()
    if approval.status != ApprovalStatus.PENDING or approval.current_step is None:
        raise ApprovalStateError("the approval is not pending")
    step = await session.scalar(
        select(ApprovalStep).where(
            ApprovalStep.approval_id == approval.id,
            ApprovalStep.position == approval.current_step,
        )
    )
    if step is None:  # pragma: no cover - invariant
        raise ApprovalStateError("the approval has no active step")
    now = await db_now(session)
    if user_id in excluded_users(approval):
        raise BadRequestError("this person cannot decide this approval")
    decided = set(
        await session.scalars(
            select(ApprovalDecision.actor_user_id).where(
                ApprovalDecision.approval_id == approval.id
            )
        )
    )
    if user_id in decided:
        raise BadRequestError("this person already decided on this approval")
    delegated = await active_delegations(
        session,
        workflow=approval.workflow,
        now=now,
        delegate_id=user_id,
        delegator_ids=step.assignee_ids,
    )
    if user_id not in step.assignee_ids and user_id not in step.escalation_ids and not delegated:
        raise BadRequestError("this person is not an approver of the active step")

    token, digest = new_token()
    link = ApprovalLink(
        id=uuid.uuid4(),
        approval_id=approval.id,
        step_id=step.id,
        user_id=user_id,
        action=action.value,
        channel=channel.value,
        token_hash=digest,
        approval_state_version=approval.state_version,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(link)
    await session.flush()
    await append_audit(
        session,
        action=Actions.LINK_ISSUED,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR_ID,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={
            "link_id": str(link.id),
            "user_id": str(user_id),
            "action": action.value,
            "channel": channel.value,
            "step": step.name,
            "expires_at": link.expires_at.isoformat(),
        },
    )
    return IssuedLink(
        id=link.id,
        token=token,
        approval_id=approval.id,
        step_id=step.id,
        user_id=user_id,
        action=action.value,
        expires_at=link.expires_at,
    )


async def link_org(session: AsyncSession, token: str) -> uuid.UUID:
    """The organisation of a link, via the SECURITY DEFINER lookup. Ends the transaction.

    The session must not be bound to an organisation yet; afterwards no transaction is
    open, so the caller can bind it (``load_actor``).
    """
    if current_org(session) is not None:
        raise RuntimeError("the link lookup must start without an organisation")
    if not is_well_formed_token(token):
        raise LinkInvalidError()
    org_id = await session.scalar(select(func.approval_link_org_id(hash_token(token))))
    await session.rollback()
    if not isinstance(org_id, uuid.UUID):
        raise LinkInvalidError()
    return org_id


@dataclass(frozen=True, slots=True)
class LinkView:
    approval_id: uuid.UUID
    title: str
    workflow: str
    approval_type: str
    step_name: str
    action: str
    expires_at: datetime
    state: LinkState
    second_factor_required: bool
    has_second_factor: bool


async def _bound_link(
    session: AsyncSession, actor: Actor, token: str, *, lock: bool
) -> ApprovalLink:
    query = select(ApprovalLink).where(ApprovalLink.token_hash == hash_token(token))
    link = await session.scalar(query.with_for_update() if lock else query)
    # Another person's link looks exactly like an unknown one.
    if link is None or link.user_id != actor.id:
        raise LinkInvalidError()
    return link


def _state(link: ApprovalLink, approval: Approval, now: datetime) -> LinkState:
    if link.used_at is not None:
        return "used"
    if link.invalidated_at is not None:
        return "invalidated"
    if now >= link.expires_at:
        return "expired"
    if (
        approval.status != ApprovalStatus.PENDING
        or approval.state_version != link.approval_state_version
    ):
        return "invalidated"
    return "valid"


async def describe_link(session: AsyncSession, actor: Actor, token: str) -> LinkView:
    """What the link would do, for its bound user. Read only: never changes state."""
    link = await _bound_link(session, actor, token, lock=False)
    approval = await session.scalar(select(Approval).where(Approval.id == link.approval_id))
    step = await session.scalar(select(ApprovalStep).where(ApprovalStep.id == link.step_id))
    if approval is None or step is None:  # pragma: no cover - foreign keys
        raise LinkInvalidError()
    now = await db_now(session)
    return LinkView(
        approval_id=approval.id,
        title=approval.title,
        workflow=approval.workflow,
        approval_type=approval.approval_type,
        step_name=step.name,
        action=link.action,
        expires_at=link.expires_at,
        state=_state(link, approval, now),
        second_factor_required=True,
        has_second_factor=actor.has_second_factor,
    )


async def confirm_link(
    session: AsyncSession,
    actor: Actor,
    token: str,
    *,
    reason: str | None,
    hooks: ApprovalHooks = NO_HOOKS,
) -> DecisionOutcome:
    """Use the link: record the bound user's decision through ``decide``. Single use.

    Repeating a confirm whose decision is already recorded returns that decision.
    """
    link = await _bound_link(session, actor, token, lock=False)
    # Lock order: approval, then link (``decide`` locks the approval and then updates
    # links), so a link confirm and an inbox decision can never deadlock.
    await session.execute(
        select(Approval.id).where(Approval.id == link.approval_id).with_for_update()
    )
    await session.refresh(link, with_for_update=True)
    if link.used_at is not None and link.decision_id is not None:
        row = await session.scalar(
            select(ApprovalDecision).where(ApprovalDecision.id == link.decision_id)
        )
        approval = await session.scalar(select(Approval).where(Approval.id == link.approval_id))
        step = await session.scalar(
            select(ApprovalStep.name).where(ApprovalStep.id == link.step_id)
        )
        if row is None or approval is None or row.actor_user_id != actor.id:
            raise LinkInvalidError()
        return DecisionOutcome(
            decision_id=row.id,
            approval_id=approval.id,
            run_id=approval.run_id,
            decision=row.decision,
            approval_status=approval.status,
            step_name=step or "",
            replayed=True,
        )
    approval = await session.scalar(select(Approval).where(Approval.id == link.approval_id))
    if approval is None:  # pragma: no cover - foreign key
        raise LinkInvalidError()
    now = await db_now(session)
    if _state(link, approval, now) != "valid":
        raise LinkInvalidError()
    outcome = await decide(
        session,
        actor,
        approval.id,
        decision=Decision(link.action),
        reason=reason,
        idempotency_key=f"link:{link.id}",
        channel=Channel(link.channel),
        expected_step_id=link.step_id,
        link_id=link.id,
        hooks=hooks,
    )
    link.used_at = now
    link.decision_id = outcome.decision_id
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        Actions.LINK_USED,
        entity_type="approval",
        entity_id=approval.id,
        workflow=approval.workflow,
        payload={"link_id": str(link.id), "decision_id": str(outcome.decision_id)},
    )
    return outcome

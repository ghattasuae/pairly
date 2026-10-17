# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Create and list proposals.

Rules:

* Creating needs ``core.proposal.propose``; listing needs ``core.proposal.read``.
* Only a person, the agent or an API key proposes (portal actors cannot hold core
  permissions at all).
* A proposal on an approval (``entity_type="approval"``) must name an approval of this
  organisation (Row Level Security hides the others: 404), and takes its workflow.
* A proposal never changes the entity. Every proposal writes one audit event.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval
from pairly_core.audit import actor_ref, audit_actor_action
from pairly_core.errors import BadRequestError, NotFoundError
from pairly_core.proposals.models import Proposal
from pairly_core.proposals.permissions import PROPOSAL_PROPOSE, PROPOSAL_READ
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError, RoleRegistry, authorize

PROPOSAL_CREATED: Final = "core.proposal.created"
APPROVAL_ENTITY: Final = "approval"
MAX_BODY: Final = 4000
_ENTITY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){0,3}$")
_ACTION_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENTITY_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PROPOSERS: Final = frozenset({ActorKind.USER, ActorKind.AGENT, ActorKind.API_KEY})


class ProposalKind(StrEnum):
    NOTE = "note"
    ACTION = "action"


@dataclass(frozen=True, slots=True)
class ProposalPage:
    items: list[Proposal]
    next_cursor: str | None


async def create_proposal(
    session: AsyncSession,
    registry: RoleRegistry,
    actor: Actor,
    *,
    entity_type: str,
    entity_id: str,
    kind: ProposalKind,
    body: str,
    action: str | None = None,
    workflow: str | None = None,
) -> Proposal:
    authorize(actor, PROPOSAL_PROPOSE)
    if actor.kind not in _PROPOSERS:
        raise PermissionDeniedError("only a person, the agent or an API key can propose")
    if len(entity_type) > 64 or not _ENTITY_RE.fullmatch(entity_type):
        raise BadRequestError("entity_type must be a lowercase name such as pay.invoice")
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        raise BadRequestError("entity_id must be 1 to 128 letters, digits or ._:-")
    text = body.strip()
    if not 1 <= len(text) <= MAX_BODY:
        raise BadRequestError(f"the proposal needs a body of 1 to {MAX_BODY} characters")
    if kind is ProposalKind.ACTION:
        if action is None or not _ACTION_RE.fullmatch(action):
            raise BadRequestError("an action proposal names its action (a lowercase identifier)")
    elif action is not None:
        raise BadRequestError("a note has no action")
    if workflow is not None and workflow not in registry.workflows:
        raise BadRequestError("unknown workflow")
    if entity_type == APPROVAL_ENTITY:
        try:
            approval_id = uuid.UUID(entity_id)
        except ValueError:
            raise BadRequestError("entity_id must be an approval id") from None
        approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
        if approval is None:
            raise NotFoundError("approval not found")
        workflow = approval.workflow
        entity_id = str(approval.id)
    by_type, by_id = actor_ref(actor)
    row = Proposal(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        workflow=workflow,
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind.value,
        action=action,
        body=text,
        proposed_by_type=by_type.value,
        proposed_by_id=by_id,
    )
    session.add(row)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        PROPOSAL_CREATED,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=workflow,
        # The body may name people: the audit event keeps its length, not its text.
        payload={
            "proposal_id": str(row.id),
            "kind": row.kind,
            "action": action,
            "body_chars": len(text),
        },
    )
    await session.refresh(row)
    return row


def _encode_cursor(created: datetime, ident: uuid.UUID) -> str:
    raw = json.dumps([created.isoformat(), str(ident)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, ident = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(created), uuid.UUID(ident)
    except (ValueError, TypeError):
        raise BadRequestError("invalid cursor") from None


async def list_proposals(
    session: AsyncSession,
    actor: Actor,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    workflow: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> ProposalPage:
    """Newest first, with an opaque cursor for the next page."""
    authorize(actor, PROPOSAL_READ)
    query = select(Proposal)
    if entity_type is not None:
        query = query.where(Proposal.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(Proposal.entity_id == entity_id)
    if workflow is not None:
        query = query.where(Proposal.workflow == workflow)
    if cursor is not None:
        created, ident = _decode_cursor(cursor)
        query = query.where(
            or_(
                Proposal.created_at < created,
                and_(Proposal.created_at == created, Proposal.id < ident),
            )
        )
    limit = max(1, min(limit, 200))
    rows = list(
        await session.scalars(
            query.order_by(Proposal.created_at.desc(), Proposal.id.desc()).limit(limit + 1)
        )
    )
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode_cursor(items[-1].created_at, items[-1].id)
    return ProposalPage(items=items, next_cursor=next_cursor)

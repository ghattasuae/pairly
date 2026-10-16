# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/proposals``: an agent or an API client suggests, a person decides.

* ``POST /v1/proposals``  a note or a suggested action on one entity
  (``core.proposal.propose``). It changes nothing else: no approval is decided, no
  payment made, nothing deleted. Safe to retry with an ``Idempotency-Key``.
* ``GET  /v1/proposals``  newest first, filtered by entity or workflow
  (``core.proposal.read``), with a cursor.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from pairly_api.idempotency import idempotent
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.proposals.models import Proposal
from pairly_core.proposals.service import (
    MAX_BODY,
    ProposalKind,
    create_proposal,
    list_proposals,
)

router = APIRouter(prefix="/proposals", tags=["proposals"])

Ident = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


EntityType = Annotated[
    str, Field(max_length=64, pattern=r"^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){0,3}$")
]


class ProposalIn(BaseModel):
    # What the proposal is about: "approval", "pay.invoice", "hire.application", ...
    entity_type: EntityType
    entity_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: ProposalKind = ProposalKind.NOTE
    # For an action proposal: what a person should consider doing, for example
    # "approve", "reject", "request_changes", "hold" or "move_to_interview".
    action: Ident | None = None
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY)]
    workflow: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None


class ProposalOut(BaseModel):
    id: uuid.UUID
    workflow: str | None
    entity_type: str
    entity_id: str
    kind: str
    action: str | None
    body: str
    proposed_by_type: str
    proposed_by_id: str
    created_at: datetime

    @classmethod
    def of(cls, row: Proposal) -> ProposalOut:
        return cls(
            id=row.id,
            workflow=row.workflow,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            kind=row.kind,
            action=row.action,
            body=row.body,
            proposed_by_type=row.proposed_by_type,
            proposed_by_id=row.proposed_by_id,
            created_at=row.created_at,
        )


class ProposalList(BaseModel):
    items: list[ProposalOut]
    next_cursor: str | None


@router.post(
    "",
    status_code=201,
    response_model=ProposalOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def propose(
    body: ProposalIn, actor: CurrentActor, session: OrgSession, ctx: Context
) -> ProposalOut:
    row = await create_proposal(
        session,
        ctx.registry,
        actor,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        kind=body.kind,
        action=body.action,
        body=body.body,
        workflow=body.workflow,
    )
    return ProposalOut.of(row)


@router.get("", response_model=ProposalList)
async def proposals(
    actor: CurrentActor,
    session: OrgSession,
    entity_type: Annotated[
        str | None, Query(max_length=64, pattern=r"^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){0,3}$")
    ] = None,
    entity_id: Annotated[str | None, Query(max_length=128)] = None,
    workflow: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ProposalList:
    page = await list_proposals(
        session,
        actor,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=workflow,
        cursor=cursor,
        limit=limit,
    )
    return ProposalList(items=[ProposalOut.of(r) for r in page.items], next_cursor=page.next_cursor)

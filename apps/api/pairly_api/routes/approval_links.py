# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Signed single-use approval links (email and Slack). See ``pairly_core.signed_links``.

* ``POST /v1/approval-links/describe``  what the link would do. Never changes state.
* ``POST /v1/approval-links/confirm``   confirm: the bound user's decision.

The token travels in the JSON body only, never in a URL, so it stays out of access logs,
traces and browser history (the same rule as invitations). Both need a bearer JWT of the
user the link was sent to; the organisation comes from the link, not from a header.
Confirming goes through the same checks as the inbox, including the TOTP step-up (a
sign-in with ``amr`` ``otp``). Any unknown, foreign, used, expired or invalidated link
gets the same 410.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_api.routes.approvals import DecisionOut, Runtime, decision_out, schedule_resume
from pairly_core.errors import ForbiddenError
from pairly_core.identity import Context, RequestSession, VerifiedSubject, load_actor
from pairly_core.rbac import Actor
from pairly_core.signed_links import LinkInvalidError, confirm_link, describe_link, link_org

router = APIRouter(prefix="/approval-links", tags=["approvals"])

# token_urlsafe(32) is 43 characters; anything else is rejected before any lookup.
Token = Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]


class LinkOut(BaseModel):
    approval_id: uuid.UUID
    title: str
    workflow: str
    approval_type: str
    step: str
    action: str
    expires_at: datetime
    state: str
    second_factor_required: bool
    has_second_factor: bool


class LinkTokenIn(BaseModel):
    token: Token


class LinkConfirmIn(LinkTokenIn):
    reason: Annotated[str | None, Field(default=None, max_length=2000)] = None


async def _link_actor(
    request: Request, ctx: Context, session: AsyncSession, verified: VerifiedSubject, token: str
) -> Actor:
    org_id = await link_org(session, token)
    try:
        actor = await load_actor(
            session, ctx.registry, subject=verified.subject, org_id=org_id, amr=verified.amr
        )
    except ForbiddenError:
        raise LinkInvalidError() from None
    request.state.actor = actor
    structlog.contextvars.bind_contextvars(user_id=str(actor.id), org_id=str(actor.org_id))
    return actor


@router.post("/describe", response_model=LinkOut)
async def describe(
    body: LinkTokenIn,
    request: Request,
    ctx: Context,
    session: RequestSession,
    verified: VerifiedSubject,
) -> LinkOut:
    actor = await _link_actor(request, ctx, session, verified, body.token)
    view = await describe_link(session, actor, body.token)
    return LinkOut(
        approval_id=view.approval_id,
        title=view.title,
        workflow=view.workflow,
        approval_type=view.approval_type,
        step=view.step_name,
        action=view.action,
        expires_at=view.expires_at,
        state=view.state,
        second_factor_required=view.second_factor_required,
        has_second_factor=view.has_second_factor,
    )


@router.post("/confirm", response_model=DecisionOut)
async def confirm(
    body: LinkConfirmIn,
    request: Request,
    ctx: Context,
    session: RequestSession,
    verified: VerifiedSubject,
    runtime: Runtime,
    background: BackgroundTasks,
) -> DecisionOut:
    actor = await _link_actor(request, ctx, session, verified, body.token)
    outcome = await confirm_link(
        session, actor, body.token, reason=body.reason, hooks=runtime.hooks
    )
    schedule_resume(background, runtime, actor, outcome)
    return decision_out(outcome)

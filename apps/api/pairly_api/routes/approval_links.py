# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Signed single-use approval links (email and Slack). See ``pairly_core.signed_links``.

* ``GET  /v1/approval-links/{token}``  what the link would do. Never changes state.
* ``POST /v1/approval-links/{token}``  confirm: the bound user's decision.

Both need a bearer JWT of the user the link was sent to; the organisation comes from the
link, not from a header. Confirming goes through the same checks as the inbox, including
the TOTP step-up (a sign-in with ``amr`` ``otp``). Any unknown, foreign, used, expired or
invalidated link gets the same 410. The token path segment is redacted from access logs
and traces (``pairly_api.middleware``, ``pairly_api.telemetry``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_api.routes.approvals import DecisionOut, Runtime, decision_out, schedule_resume
from pairly_core.errors import ForbiddenError
from pairly_core.identity import Context, RequestSession, VerifiedSubject, load_actor
from pairly_core.rbac import Actor
from pairly_core.signed_links import LinkInvalidError, confirm_link, describe_link, link_org

router = APIRouter(prefix="/approval-links", tags=["approvals"])

Token = Annotated[str, Path(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]


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


class LinkConfirmIn(BaseModel):
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


@router.get("/{token}", response_model=LinkOut)
async def describe(
    token: Token,
    request: Request,
    ctx: Context,
    session: RequestSession,
    verified: VerifiedSubject,
) -> LinkOut:
    actor = await _link_actor(request, ctx, session, verified, token)
    view = await describe_link(session, actor, token)
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


@router.post("/{token}", response_model=DecisionOut)
async def confirm(
    token: Token,
    body: LinkConfirmIn,
    request: Request,
    ctx: Context,
    session: RequestSession,
    verified: VerifiedSubject,
    runtime: Runtime,
    background: BackgroundTasks,
) -> DecisionOut:
    actor = await _link_actor(request, ctx, session, verified, token)
    outcome = await confirm_link(session, actor, token, reason=body.reason, hooks=runtime.hooks)
    schedule_resume(background, runtime, actor, outcome)
    return decision_out(outcome)

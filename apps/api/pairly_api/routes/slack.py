# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Slack integration routes (WP1.9). See ``pairly_core.integrations.slack``.

Admin (``core.integration.manage``, step-up):

* ``GET|PUT /v1/integrations/slack``          the workspace, bot token, channel mappings

Slack calls (no bearer token; every request must carry a valid Slack signature over the
raw body, at most five minutes old, or it gets 401 before anything is parsed):

* ``POST /v1/integrations/slack/interactions``   button clicks (``block_actions``)
* ``POST /v1/integrations/slack/commands``       ``/pairly connect``

Signed-in user:

* ``POST   /v1/integrations/slack/connect/confirm``  finish the connect flow (token in body)
* ``DELETE /v1/integrations/slack/link``             remove the caller's Slack link

Without ``PAIRLY_SLACK_SIGNING_SECRET`` the Slack endpoints answer 503.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from pairly_api.routes.notifications import NotifySettings
from pairly_core.errors import ForbiddenError, ServiceUnavailableError, UnauthenticatedError
from pairly_core.identity import (
    Context,
    CurrentActor,
    OrgSession,
    RequestSession,
    VerifiedSubject,
    load_actor,
)
from pairly_core.integrations.slack.service import (
    SlackConnectInvalidError,
    configure_slack,
    confirm_connect,
    connect_org,
    get_slack,
    handle_interaction,
    parse_interaction,
    start_connect,
    unlink_user,
)
from pairly_core.integrations.slack.signature import (
    MAX_BODY_BYTES,
    SlackSignatureError,
    verify,
)
from pairly_core.integrations.slack.transport import SlackTransport
from pairly_core.notifications.email import DeliveryError
from pairly_core.notifications.permissions import INTEGRATION_MANAGE
from pairly_core.rbac import Actor, require

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/integrations/slack", tags=["integrations"])

Manager = Annotated[Actor, Depends(require(INTEGRATION_MANAGE))]


class SlackIn(BaseModel):
    team_id: Annotated[str, Field(pattern=r"^T[A-Z0-9]{2,30}$")]
    bot_token: Annotated[str, Field(min_length=10, max_length=260)]
    channel_mappings: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class SlackOut(BaseModel):
    team_id: str
    status: str
    channel_mappings: dict[str, str]
    updated_at: datetime | None


class ConnectIn(BaseModel):
    token: Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]


@router.get("", response_model=SlackOut | None)
async def get_integration(actor: Manager, session: OrgSession) -> SlackOut | None:
    row = await get_slack(session, actor)
    if row is None:
        return None
    return SlackOut(
        team_id=row.external_id,
        status=row.status,
        channel_mappings=dict(row.settings.get("channel_mappings", {})),
        updated_at=row.updated_at,
    )


@router.put("", response_model=SlackOut)
async def put_integration(body: SlackIn, actor: Manager, session: OrgSession) -> SlackOut:
    row = await configure_slack(
        session,
        actor,
        team_id=body.team_id,
        bot_token=body.bot_token,
        channel_mappings=body.channel_mappings,
        enabled=body.enabled,
    )
    return SlackOut(
        team_id=row.external_id,
        status=row.status,
        channel_mappings=dict(row.settings.get("channel_mappings", {})),
        updated_at=row.updated_at,
    )


# ------------------------------------------------------------------ Slack requests


def get_slack_transport(request: Request) -> SlackTransport:
    transport = getattr(request.app.state, "slack_transport", None)
    if transport is None:
        raise ServiceUnavailableError("Slack is not configured")
    return transport  # type: ignore[no-any-return]


async def _verified_form(request: Request, settings: NotifySettings) -> dict[str, str]:
    """The Slack form body, after the signature check over the raw bytes."""
    secret = settings.slack_signing_secret
    if secret is None:
        raise ServiceUnavailableError("Slack is not configured")
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        raise UnauthenticatedError("invalid Slack request")
    body = await request.body()
    try:
        verify(
            signing_secret=secret.get_secret_value(),
            timestamp=request.headers.get("x-slack-request-timestamp"),
            signature=request.headers.get("x-slack-signature"),
            body=body,
            window_seconds=settings.slack_timestamp_window_seconds,
        )
    except SlackSignatureError as exc:
        log.warning("slack_signature_rejected", reason=exc.reason)
        raise UnauthenticatedError("invalid Slack request") from None
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=50)
    except (UnicodeDecodeError, ValueError):
        return {}
    return {k: v[0] for k, v in fields.items() if v}


@router.post("/interactions")
async def interactions(
    request: Request,
    ctx: Context,
    settings: NotifySettings,
    transport: Annotated[SlackTransport, Depends(get_slack_transport)],
) -> Response:
    form = await _verified_form(request, settings)
    try:
        payload: Any = json.loads(form.get("payload", ""))
    except ValueError:
        payload = None
    interaction = parse_interaction(payload) if isinstance(payload, dict) else None
    if interaction is None:
        return Response(status_code=200)
    reply = await handle_interaction(ctx.sessionmaker, interaction, web_url=settings.web_url)
    if reply.text and interaction.response_url:
        try:
            await transport.respond(
                interaction.response_url,
                {"response_type": "ephemeral", "replace_original": False, "text": reply.text},
            )
        except DeliveryError as exc:
            log.warning("slack_respond_failed", code=exc.code)
    return JSONResponse({"ok": True, "outcome": reply.outcome})


@router.post("/commands")
async def commands(request: Request, ctx: Context, settings: NotifySettings) -> JSONResponse:
    form = await _verified_form(request, settings)
    text = (form.get("text") or "").strip().lower()
    if text in {"connect", ""}:
        reply = await start_connect(
            ctx.sessionmaker,
            team_id=form.get("team_id", ""),
            slack_user_id=form.get("user_id", ""),
            web_url=settings.web_url,
        )
        return JSONResponse({"response_type": "ephemeral", "text": reply.text})
    return JSONResponse(
        {
            "response_type": "ephemeral",
            "text": "Use /pairly connect to link your Slack account to Pairly.",
        }
    )


# ------------------------------------------------------------------ signed-in user


@router.post("/connect/confirm")
async def connect_confirm(
    body: ConnectIn,
    request: Request,
    ctx: Context,
    session: RequestSession,
    verified: VerifiedSubject,
) -> dict[str, Any]:
    org_id = await connect_org(session, body.token)
    try:
        actor = await load_actor(
            session, ctx.registry, subject=verified.subject, org_id=org_id, amr=verified.amr
        )
    except ForbiddenError:
        raise SlackConnectInvalidError() from None
    request.state.actor = actor
    link = await confirm_connect(session, actor, body.token)
    return {"linked": True, "team_id": link.team_id, "slack_user_id": link.slack_user_id}


@router.delete("/link", status_code=204)
async def unlink(actor: CurrentActor, session: OrgSession) -> Response:
    await unlink_user(session, actor)
    return Response(status_code=204)

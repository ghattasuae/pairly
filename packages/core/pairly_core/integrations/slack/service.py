# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Slack integration: configuration, the signed connect flow, and interactions.

Configuration (``configure_slack``): an admin with ``core.integration.manage`` (step-up)
stores the workspace (team id), the bot token (encrypted) and channel mappings. A Slack
workspace belongs to one organisation at most (unique constraint).

Connect flow (links a Slack user to a Pairly user, both sides proven):

1. In Slack the person runs ``/pairly connect``. The request is signed by Slack
   (``signature.verify``), so the Slack user id and team id are Slack's word. We store a
   connect request with a random single-use token (SHA-256 only, 10 minute expiry) and
   answer, only to that person (ephemeral), with ``/integrations/slack/connect#<token>``
   on the web app (token in the fragment).
2. The web page calls ``POST /v1/integrations/slack/connect/confirm`` with the token in
   the body and the person's bearer JWT. The link is made for the signed-in user of that
   token's organisation, if they are a member. One Slack user per Pairly user per
   organisation; reconnecting replaces the old link.

Interactions (``handle_interaction``), after the HTTP layer verified the signature:

* The team id maps to one organisation (a SECURITY DEFINER lookup); unknown teams get a
  neutral answer.
* Idempotent by Slack's ``action_ts`` per Slack user: a retried delivery is answered
  ``duplicate`` and does nothing.
* The Slack user must have a verified link; otherwise nothing happens and they are told
  to connect first.
* The action value must be a signed approval link token whose bound person is that
  linked user. A bare approval id is never accepted.
* ``approve`` and ``reject`` never decide here (see the package docstring): they are
  link buttons that open the web confirmation page; we only audit the click.
* ``remind_later`` (no step-up needed) schedules a Slack reminder in one hour.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.models import Approval, ApprovalLink, ApprovalStatus
from pairly_core.approvals.service import db_now
from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.db import current_org, set_tenant
from pairly_core.errors import BadRequestError, NotFoundError, PairlyError
from pairly_core.integrations.models import (
    Integration,
    SlackInteraction,
    SlackLinkRequest,
    SlackUserLink,
)
from pairly_core.integrations.slack.blocks import NATIVE_ACTIONS, REMIND_LATER, WEB_ACTIONS
from pairly_core.integrations.slack.links import SLACK, user_for_slack
from pairly_core.notifications.links import absolute_url, approval_path
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import Actor, authorize
from pairly_core.signed_links import hash_token, is_well_formed_token

log = structlog.get_logger(__name__)

MANAGE: Final = "core.integration.manage"
SLACK_ACTOR: Final = "pairly.integrations.slack"
CONNECT_TTL: Final = timedelta(minutes=10)
REMIND_AFTER: Final = timedelta(hours=1)
_TEAM_RE = re.compile(r"^T[A-Z0-9]{2,30}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{2,30}$")
_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{2,30}$")
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ACTION_TS_RE = re.compile(r"^[0-9]{1,12}\.[0-9]{1,8}$")
_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]{10,250}$")


class SlackActions:
    CONFIGURED = "core.slack.configured"
    CONNECT_STARTED = "core.slack.connect_started"
    USER_LINKED = "core.slack.user_linked"
    USER_UNLINKED = "core.slack.user_unlinked"
    ACTION = "core.slack.action"


class SlackConnectInvalidError(PairlyError):
    status = 410
    code = "slack_connect_invalid"
    title = "This connect link is no longer valid"


def valid_team(team_id: object) -> bool:
    return isinstance(team_id, str) and _TEAM_RE.fullmatch(team_id) is not None


def valid_user(user_id: object) -> bool:
    return isinstance(user_id, str) and _USER_RE.fullmatch(user_id) is not None


# ------------------------------------------------------------------ configuration


async def configure_slack(
    session: AsyncSession,
    actor: Actor,
    *,
    team_id: str,
    bot_token: str,
    channel_mappings: Mapping[str, str],
    enabled: bool = True,
) -> Integration:
    authorize(actor, MANAGE)
    if not valid_team(team_id):
        raise BadRequestError("invalid Slack team id")
    if not _BOT_TOKEN_RE.fullmatch(bot_token):
        raise BadRequestError("invalid Slack bot token")
    mappings: dict[str, str] = {}
    for category, channel in channel_mappings.items():
        if not _CATEGORY_RE.fullmatch(category) or not _CHANNEL_RE.fullmatch(channel):
            raise BadRequestError("invalid channel mapping")
        mappings[category] = channel
    row = await session.scalar(select(Integration).where(Integration.provider == SLACK))
    now = await db_now(session)
    if row is None:
        row = Integration(id=uuid.uuid4(), org_id=actor.org_id, provider=SLACK, created_by=actor.id)
        session.add(row)
    elif row.external_id != team_id:
        # Another workspace: links made for the old one no longer apply.
        await session.execute(delete(SlackUserLink).where(SlackUserLink.team_id == row.external_id))
    row.external_id = team_id
    row.credential = seal(Integration, "credential", bot_token, org_id=actor.org_id, row_id=row.id)  # type: ignore[assignment]
    row.settings = {"channel_mappings": mappings}
    row.status = "active" if enabled else "disabled"
    row.updated_at = now
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise BadRequestError("this Slack workspace is connected to another organisation") from None
    await audit_actor_action(
        session,
        actor,
        SlackActions.CONFIGURED,
        entity_type="integration",
        entity_id=row.id,
        payload={"team_id": team_id, "status": row.status, "mappings": sorted(mappings)},
    )
    return row


async def get_slack(session: AsyncSession, actor: Actor) -> Integration | None:
    authorize(actor, MANAGE)
    return await session.scalar(select(Integration).where(Integration.provider == SLACK))


# ------------------------------------------------------------------ org lookups


async def _definer_org(session: AsyncSession, function: str, argument: str) -> uuid.UUID | None:
    """Organisation lookup through a SECURITY DEFINER function. Ends the transaction."""
    if current_org(session) is not None:
        raise RuntimeError("the lookup must start without an organisation")
    fn = {"team": func.slack_team_org_id, "request": func.slack_link_request_org_id}[function]
    org_id = await session.scalar(select(fn(argument)))
    await session.rollback()
    return org_id if isinstance(org_id, uuid.UUID) else None


async def org_for_team(
    sessionmaker: async_sessionmaker[AsyncSession], team_id: str
) -> uuid.UUID | None:
    if not valid_team(team_id):
        return None
    async with sessionmaker() as session:
        return await _definer_org(session, "team", team_id)


# ------------------------------------------------------------------ connect flow


@dataclass(frozen=True, slots=True)
class SlackReply:
    """What to answer Slack (ephemeral text) and what happened, for the audit and tests."""

    text: str
    outcome: str


async def start_connect(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    team_id: str,
    slack_user_id: str,
    web_url: str,
) -> SlackReply:
    org_id = await org_for_team(sessionmaker, team_id)
    if org_id is None or not valid_user(slack_user_id):
        return SlackReply("This Slack workspace is not connected to Pairly.", "unknown_team")
    token = secrets.token_urlsafe(32)
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            now = await db_now(session)
            row = SlackLinkRequest(
                id=uuid.uuid4(),
                org_id=org_id,
                team_id=team_id,
                slack_user_id=slack_user_id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=now + CONNECT_TTL,
            )
            session.add(row)
            await session.flush()
            await append_audit(
                session,
                action=SlackActions.CONNECT_STARTED,
                actor_type=ActorType.SYSTEM,
                actor_id=SLACK_ACTOR,
                entity_type="slack_link_request",
                entity_id=row.id,
                payload={"team_id": team_id, "slack_user_id": slack_user_id},
            )
    url = f"{web_url}/integrations/slack/connect#{token}"
    return SlackReply(
        f"Open this link to connect your Slack account to Pairly. It works once and expires "
        f"in 10 minutes: {url}",
        "connect_started",
    )


async def connect_org(session: AsyncSession, token: str) -> uuid.UUID:
    """The organisation of a connect token. Ends the transaction (see ``link_org``)."""
    if not is_well_formed_token(token):
        raise SlackConnectInvalidError()
    org_id = await _definer_org(session, "request", hash_token(token))
    if org_id is None:
        raise SlackConnectInvalidError()
    return org_id


async def confirm_connect(session: AsyncSession, actor: Actor, token: str) -> SlackUserLink:
    """Link the token's Slack user to ``actor`` (the signed-in user). Single use."""
    request = await session.scalar(
        select(SlackLinkRequest)
        .where(SlackLinkRequest.token_hash == hash_token(token))
        .with_for_update()
    )
    now = await db_now(session)
    if request is None or request.used_at is not None or request.expires_at <= now:
        raise SlackConnectInvalidError()
    integration = await session.scalar(
        select(Integration).where(
            Integration.provider == SLACK,
            Integration.external_id == request.team_id,
            Integration.status == "active",
        )
    )
    if integration is None:
        raise SlackConnectInvalidError()
    await session.execute(
        delete(SlackUserLink).where(
            (SlackUserLink.user_id == actor.id)
            | (
                (SlackUserLink.team_id == request.team_id)
                & (SlackUserLink.slack_user_id == request.slack_user_id)
            )
        )
    )
    link = SlackUserLink(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        user_id=actor.id,
        team_id=request.team_id,
        slack_user_id=request.slack_user_id,
    )
    session.add(link)
    request.used_at = now
    request.used_by = actor.id
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        SlackActions.USER_LINKED,
        entity_type="slack_user_link",
        entity_id=link.id,
        payload={"team_id": request.team_id, "slack_user_id": request.slack_user_id},
    )
    return link


async def unlink_user(session: AsyncSession, actor: Actor) -> bool:
    link = await session.scalar(select(SlackUserLink).where(SlackUserLink.user_id == actor.id))
    if link is None:
        raise NotFoundError("no Slack account is linked")
    await session.delete(link)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        SlackActions.USER_UNLINKED,
        entity_type="slack_user_link",
        entity_id=link.id,
        payload={"team_id": link.team_id},
    )
    return True


# ------------------------------------------------------------------ interactions


@dataclass(frozen=True, slots=True)
class Interaction:
    team_id: str
    slack_user_id: str
    action_id: str
    action_ts: str
    value: str
    response_url: str | None


def parse_interaction(payload: Mapping[str, Any]) -> Interaction | None:
    """The first action of a ``block_actions`` payload, or None if it is not one."""
    if payload.get("type") != "block_actions":
        return None
    team = payload.get("team") or {}
    user = payload.get("user") or {}
    actions = payload.get("actions") or []
    if (
        not isinstance(team, Mapping)
        or not isinstance(user, Mapping)
        or not isinstance(actions, list)
    ):
        return None
    if not actions or not isinstance(actions[0], Mapping):
        return None
    action = actions[0]
    team_id, user_id = team.get("id"), user.get("id")
    action_id, action_ts, value = (
        action.get("action_id"),
        action.get("action_ts"),
        action.get("value"),
    )
    response_url = payload.get("response_url")
    if not (valid_team(team_id) and valid_user(user_id)):
        return None
    if not isinstance(action_id, str) or not re.fullmatch(r"[a-z_]{1,64}", action_id):
        return None
    if not isinstance(action_ts, str) or not _ACTION_TS_RE.fullmatch(action_ts):
        return None
    return Interaction(
        team_id=str(team_id),
        slack_user_id=str(user_id),
        action_id=action_id,
        action_ts=action_ts,
        value=value if isinstance(value, str) else "",
        response_url=response_url if isinstance(response_url, str) else None,
    )


_REPLIES: Final[dict[str, str]] = {
    "unknown_team": "This Slack workspace is not connected to Pairly.",
    "not_linked": "Connect your Slack account to Pairly first: run /pairly connect.",
    "invalid_token": "This message is out of date. Open the approval in Pairly.",
    "opened_web": "Confirm your decision on the Pairly page that just opened.",
    "reminder_scheduled": "OK. I will remind you in one hour.",
    "approval_closed": "This approval is already closed.",
    "duplicate": "",
    "unknown_action": "This action is not available.",
}


async def handle_interaction(
    sessionmaker: async_sessionmaker[AsyncSession],
    interaction: Interaction,
    *,
    web_url: str,
) -> SlackReply:
    """Handle one verified ``block_actions`` interaction. Never decides an approval."""
    org_id = await org_for_team(sessionmaker, interaction.team_id)
    if org_id is None:
        return SlackReply(_REPLIES["unknown_team"], "unknown_team")
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            seen = await session.scalar(
                select(SlackInteraction.outcome).where(
                    SlackInteraction.team_id == interaction.team_id,
                    SlackInteraction.slack_user_id == interaction.slack_user_id,
                    SlackInteraction.action_ts == interaction.action_ts,
                )
            )
            if seen is not None:
                return SlackReply(_REPLIES["duplicate"], "duplicate")
            outcome, user_id, approval_id = await _act(session, interaction, org_id, web_url)
            row = SlackInteraction(
                id=uuid.uuid4(),
                org_id=org_id,
                team_id=interaction.team_id,
                slack_user_id=interaction.slack_user_id,
                action_ts=interaction.action_ts,
                action_id=interaction.action_id,
                user_id=user_id,
                approval_id=approval_id,
                outcome=outcome,
            )
            session.add(row)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:
                # A concurrent delivery of the same action won the race.
                return SlackReply(_REPLIES["duplicate"], "duplicate")
            await append_audit(
                session,
                action=SlackActions.ACTION,
                actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
                actor_id=str(user_id) if user_id else SLACK_ACTOR,
                entity_type="approval" if approval_id else "slack_interaction",
                entity_id=approval_id or row.id,
                payload={
                    "action": interaction.action_id,
                    "outcome": outcome,
                    "slack_user_id": interaction.slack_user_id,
                    "action_ts": interaction.action_ts,
                },
            )
    return SlackReply(_REPLIES.get(outcome, ""), outcome)


async def _act(
    session: AsyncSession, interaction: Interaction, org_id: uuid.UUID, web_url: str
) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    link = await user_for_slack(session, interaction.team_id, interaction.slack_user_id)
    if link is None:
        return "not_linked", None, None
    if interaction.action_id not in WEB_ACTIONS | NATIVE_ACTIONS:
        return "unknown_action", link.user_id, None
    if not is_well_formed_token(interaction.value):
        return "invalid_token", link.user_id, None
    approval_link = await session.scalar(
        select(ApprovalLink).where(ApprovalLink.token_hash == hash_token(interaction.value))
    )
    # The token must be bound to the very person this Slack user verified as.
    if approval_link is None or approval_link.user_id != link.user_id:
        return "invalid_token", link.user_id, None
    if interaction.action_id in WEB_ACTIONS:
        # Step-up actions: decided on the web confirmation page only. Nothing changes here.
        return "opened_web", link.user_id, approval_link.approval_id
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_link.approval_id)
    )
    if approval is None or approval.status != ApprovalStatus.PENDING.value:
        return "approval_closed", link.user_id, approval_link.approval_id
    if interaction.action_id == REMIND_LATER:
        now = await db_now(session)
        await notify(
            session,
            recipient=Recipient.user(link.user_id),
            template_key="core.approval.reminder",
            variables={
                "title": approval.title,
                "inbox_url": absolute_url(web_url, approval_path(approval.id)),
            },
            dedupe_key=f"reminder:{approval.id}:{link.user_id}:{interaction.action_ts}",
            channels=["slack"],
            extras=Extras(link=approval_path(approval.id)),
            workflow=approval.workflow,
            not_before=now + REMIND_AFTER,
        )
        return "reminder_scheduled", link.user_id, approval.id
    return "unknown_action", link.user_id, None  # pragma: no cover - guarded above

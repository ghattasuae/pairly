# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Slack over HTTP against Postgres (WP1.9): configuration, the signed connect flow,
approval messages with link buttons, native "remind me", idempotency and signatures.

Slack itself is never called: the app gets a ``FakeSlackTransport``, and requests that
"come from Slack" are signed here with the test signing secret."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.integrations.slack.signature import compute
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.email import FakeEmailTransport
from pairly_core.signed_links import hash_token

from .conftest import DbUrls, TokenFactory
from .notify_support import (
    SLACK_SECRET,
    WEB_URL,
    AppFactory,
    NotifyApi,
    notifications,
    notify_api,
    notify_settings,
)
from .wf_support import (
    World,
    audit_actions,
    decide_as,
    other_world,
    owner_sql,
    start,
    world,
)

__all__ = ["notify_api", "other_world", "world"]  # fixtures

BOT_TOKEN = "xoxb-1234567890-test-bot-token"  # noqa: S105 (test value)
RESPONSE_URL = "https://hooks.slack.com/actions/T0/1/test"
CONNECT_RE = re.compile(r"/integrations/slack/connect#([A-Za-z0-9_-]{43})")


def _slack_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10].upper()


def _signed(
    form: dict[str, str],
    *,
    secret: str = SLACK_SECRET,
    age: int = 0,
    tamper: bool = False,
) -> tuple[bytes, dict[str, str]]:
    body = urlencode(form).encode()
    ts = str(int(time.time()) - age)
    signature = compute(secret, ts, body)
    if tamper:
        body += b"&extra=1"
    return body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": signature,
    }


async def _post_signed(
    api: NotifyApi, path: str, form: dict[str, str], **kw: Any
) -> httpx.Response:
    body, headers = _signed(form, **kw)
    return await api.client.post(f"/v1/integrations/slack/{path}", content=body, headers=headers)


async def _configure(api: NotifyApi, world: World) -> str:
    team = _slack_id("T")
    response = await api.client.put(
        "/v1/integrations/slack",
        json={"team_id": team, "bot_token": BOT_TOKEN, "channel_mappings": {"sla": "C0TEST01"}},
        headers=api.headers(world, "admin"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    return team


async def _connect_token(api: NotifyApi, team: str, slack_user: str) -> str:
    response = await _post_signed(
        api,
        "commands",
        {"team_id": team, "user_id": slack_user, "command": "/pairly", "text": "connect"},
    )
    assert response.status_code == 200, response.text
    found = CONNECT_RE.search(response.json()["text"])
    assert found is not None, response.text
    assert response.json()["response_type"] == "ephemeral"
    return found.group(1)


async def _confirm(api: NotifyApi, world: World, name: str, token: str) -> httpx.Response:
    return await api.client.post(
        "/v1/integrations/slack/connect/confirm",
        json={"token": token},
        headers=api.headers(world, name),
    )


async def _connect(api: NotifyApi, world: World, name: str, team: str) -> str:
    slack_user = _slack_id("U")
    response = await _confirm(api, world, name, await _connect_token(api, team, slack_user))
    assert response.status_code == 200, response.text
    assert response.json() == {"linked": True, "team_id": team, "slack_user_id": slack_user}
    return slack_user


def _payload(
    team: str,
    slack_user: str,
    action_id: str,
    value: str,
    *,
    action_ts: str | None = None,
    response_url: str = RESPONSE_URL,
) -> dict[str, str]:
    payload = {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": slack_user},
        "response_url": response_url,
        "actions": [
            {
                "action_id": action_id,
                "action_ts": action_ts or f"{int(time.time())}.{uuid.uuid4().int % 10**6:06d}",
                "value": value,
            }
        ],
    }
    return {"payload": json.dumps(payload)}


async def _click(api: NotifyApi, *args: Any, **kwargs: Any) -> str:
    response = await _post_signed(api, "interactions", _payload(*args, **kwargs))
    assert response.status_code == 200, response.text
    return str(response.json()["outcome"])


# ------------------------------------------------------------------ connect flow


async def test_signed_connect_flow(
    notify_api: NotifyApi, world: World, other_world: World, owner_engine: AsyncEngine
) -> None:
    api = notify_api
    team = await _configure(api, world)
    # Configuration is admin only, with a step-up sign-in.
    assert (
        await api.client.get("/v1/integrations/slack", headers=api.headers(world, "appr1"))
    ).status_code == 403
    assert (
        await api.client.get(
            "/v1/integrations/slack", headers=api.headers(world, "admin", otp=False)
        )
    ).status_code == 403
    shown = await api.client.get("/v1/integrations/slack", headers=api.headers(world, "admin"))
    assert shown.json()["channel_mappings"] == {"sla": "C0TEST01"}
    assert BOT_TOKEN not in shown.text

    slack_user = _slack_id("U")
    token = await _connect_token(api, team, slack_user)
    # Not a member of the token's organisation: refused, the request stays unused.
    assert (await _confirm(api, other_world, "appr1", token)).status_code == 410
    linked = await _confirm(api, world, "appr1", token)
    assert linked.status_code == 200, linked.text
    rows = await owner_sql(
        owner_engine, world.org_id, "SELECT user_id, team_id, slack_user_id FROM slack_user_links"
    )
    assert [(r.user_id, r.team_id, r.slack_user_id) for r in rows] == [
        (world.id("appr1"), team, slack_user)
    ]
    # Single use.
    assert (await _confirm(api, world, "appr2", token)).status_code == 410
    # Expired.
    late = await _connect_token(api, team, _slack_id("U"))
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE slack_link_requests SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '30 minutes' WHERE token_hash = :h",
        {"h": hash_token(late)},
    )
    assert (await _confirm(api, world, "appr2", late)).status_code == 410
    # Unknown and malformed tokens.
    assert (await _confirm(api, world, "appr2", "A" * 43)).status_code == 410
    assert (await _confirm(api, world, "appr2", "short")).status_code == 422
    assert (
        await api.client.post("/v1/integrations/slack/connect/confirm", json={"token": "A" * 43})
    ).status_code == 401
    # A workspace that is not connected gets a neutral answer and no request.
    unknown = await _post_signed(
        api, "commands", {"team_id": _slack_id("T"), "user_id": slack_user, "text": "connect"}
    )
    assert "not connected" in unknown.json()["text"]
    other = await _post_signed(
        api, "commands", {"team_id": team, "user_id": slack_user, "text": "help"}
    )
    assert "/pairly connect" in other.json()["text"]

    # Reconnecting replaces the link; unlinking removes it.
    second = await _connect(api, world, "appr1", team)
    rows = await owner_sql(owner_engine, world.org_id, "SELECT slack_user_id FROM slack_user_links")
    assert [r.slack_user_id for r in rows] == [second]
    headers = api.headers(world, "appr1")
    assert (
        await api.client.delete("/v1/integrations/slack/link", headers=headers)
    ).status_code == 204
    assert (
        await api.client.delete("/v1/integrations/slack/link", headers=headers)
    ).status_code == 404
    actions = await audit_actions(owner_engine, world.org_id)
    for action in (
        "core.slack.configured",
        "core.slack.connect_started",
        "core.slack.user_linked",
        "core.slack.user_unlinked",
    ):
        assert action in actions, action


# ------------------------------------------------------------------ approval messages


async def test_approval_buttons_open_the_web_page_and_remind_later_is_native(
    notify_api: NotifyApi, world: World, other_world: World, owner_engine: AsyncEngine
) -> None:
    api = notify_api
    team = await _configure(api, world)
    u1 = await _connect(api, world, "appr1", team)
    u2 = await _connect(api, world, "appr2", team)
    run = await start(api.runtime, world)
    approval_id = run.pending_approval_id

    ((row, content),) = [
        (r, c)
        for r, c in await notifications(api.sessionmaker, world.org_id, channel="slack")
        if r.recipient_user_id == world.id("appr1")
    ]
    assert row.status == "queued"
    (actions_block,) = [b for b in content["blocks"] if b["type"] == "actions"]
    buttons = {e["action_id"]: e for e in actions_block["elements"]}
    assert set(buttons) == {"approve", "reject", "remind_later"}
    for name, style in (("approve", "primary"), ("reject", "danger")):
        button = buttons[name]
        # A link button: it opens the web confirmation page with the token in the fragment.
        assert button["url"] == f"{WEB_URL}/approvals/link#{button['value']}"
        assert button["style"] == style
    assert "url" not in buttons["remind_later"]
    approve_token = buttons["approve"]["value"]
    assert buttons["remind_later"]["value"] == approve_token
    links = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT user_id, action, channel FROM approval_links WHERE token_hash = :h",
        {"h": hash_token(approve_token)},
    )
    assert [(r.user_id, r.action, r.channel) for r in links] == [
        (world.id("appr1"), "approve", "slack_link")
    ]
    assert any(b["type"] == "context" for b in content["blocks"])  # the "confirm on web" note

    # Delivery posts to the person's Slack id with the organisation's bot token.
    slack = FakeSlackTransport()
    transports = Transports(settings=api.settings, email=FakeEmailTransport(), slack=slack)
    await deliver_org(api.sessionmaker, world.org_id, transports)
    assert {p["channel"] for p in slack.posts} >= {u1, u2}
    assert {p["token"] for p in slack.posts} == {BOT_TOKEN}

    # Approve and reject never decide in Slack.
    assert await _click(api, team, u1, "approve", approve_token) == "opened_web"
    assert await _click(api, team, u1, "reject", buttons["reject"]["value"]) == "opened_web"
    decisions = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM approval_decisions WHERE approval_id = :a",
        {"a": approval_id},
    )
    assert decisions[0].n == 0
    (status,) = await owner_sql(
        owner_engine, world.org_id, "SELECT status FROM approvals WHERE id = :a", {"a": approval_id}
    )
    assert status.status == "pending"
    assert api.slack.responses[-1][0] == RESPONSE_URL
    assert "Pairly page" in api.slack.responses[-1][1]["text"]

    # Remind me: handled natively, once per Slack action.
    ts = f"{int(time.time())}.000777"
    assert await _click(api, team, u1, "remind_later", approve_token, action_ts=ts) == (
        "reminder_scheduled"
    )
    replies = len(api.slack.responses)
    assert await _click(api, team, u1, "remind_later", approve_token, action_ts=ts) == "duplicate"
    assert len(api.slack.responses) == replies  # nothing to say to a retried delivery
    reminders = await notifications(
        api.sessionmaker, world.org_id, template_key="core.approval.reminder"
    )
    assert len(reminders) == 1
    reminder = reminders[0][0]
    assert (reminder.channel, reminder.status, reminder.reason) == ("slack", "queued", "scheduled")
    assert reminder.recipient_user_id == world.id("appr1")
    assert reminder.next_attempt_at is not None
    assert reminder.next_attempt_at > datetime.now(UTC) + timedelta(minutes=55)

    # Refusals: an unlinked Slack user, someone else's token, unknown tokens and actions,
    # and a workspace that is not connected.
    assert await _click(api, team, _slack_id("U"), "approve", approve_token) == "not_linked"
    assert await _click(api, team, u2, "remind_later", approve_token) == "invalid_token"
    assert await _click(api, team, u2, "approve", approve_token) == "invalid_token"
    assert await _click(api, team, u1, "approve", "A" * 43) == "invalid_token"
    assert await _click(api, team, u1, "remind_later", str(approval_id)) == "invalid_token"
    assert await _click(api, team, u1, "delete_everything", approve_token) == "unknown_action"
    assert await _click(api, _slack_id("T"), u1, "approve", approve_token) == "unknown_team"
    # A forged response_url is never called (no SSRF); the click is still answered.
    before = len(api.slack.responses)
    outcome = await _click(
        api, team, u1, "approve", approve_token, response_url="https://evil.example/hook"
    )
    assert outcome == "opened_web"
    assert len(api.slack.responses) == before

    # Once the approval is closed, "remind me" does nothing.
    await decide_as(api.runtime, world, "appr2", approval_id)
    assert await _click(api, team, u1, "remind_later", approve_token) == "approval_closed"
    assert (
        len(
            await notifications(
                api.sessionmaker, world.org_id, template_key="core.approval.reminder"
            )
        )
        == 1
    )
    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("core.slack.action") >= 10
    # Nothing leaked into the other organisation.
    assert await notifications(api.sessionmaker, other_world.org_id, channel="slack") == []


# ------------------------------------------------------------------ signatures


async def test_slack_requests_must_be_signed_and_fresh(notify_api: NotifyApi, world: World) -> None:
    api = notify_api
    team = await _configure(api, world)
    form = _payload(team, _slack_id("U"), "approve", "A" * 43)
    assert (await _post_signed(api, "interactions", form)).status_code == 200
    for kw in ({"age": 600}, {"tamper": True}, {"secret": "not-the-secret-" + "z" * 20}):
        response = await _post_signed(api, "interactions", form, **kw)
        assert response.status_code == 401, kw
        response = await _post_signed(api, "commands", {"team_id": team, "text": "connect"}, **kw)
        assert response.status_code == 401, kw
    body, headers = _signed(form)
    for missing in ("X-Slack-Signature", "X-Slack-Request-Timestamp"):
        partial = {k: v for k, v in headers.items() if k != missing}
        response = await api.client.post(
            "/v1/integrations/slack/interactions", content=body, headers=partial
        )
        assert response.status_code == 401, missing
    # Signed but meaningless: acknowledged, nothing happens.
    assert (await _post_signed(api, "interactions", {"payload": "not json"})).status_code == 200
    assert (
        await _post_signed(
            api, "interactions", {"payload": json.dumps({"type": "view_submission"})}
        )
    ).status_code == 200


async def test_slack_answers_503_without_a_signing_secret(
    migrated: DbUrls, tokens: TokenFactory
) -> None:
    async for api in AppFactory(migrated, tokens)(notify_settings(slack_signing_secret=None)):
        form = _payload(_slack_id("T"), _slack_id("U"), "approve", "A" * 43)
        for path in ("interactions", "commands"):
            response = await _post_signed(api, path, form)
            assert response.status_code == 503, path

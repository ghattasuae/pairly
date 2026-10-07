# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notifications against Postgres (WP1.9): approval messages, preferences, digests,
unsubscribe, delivery retries, sensitive templates, the in-app feed, live updates and
the template API."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_core.approvals.models import Decision
from pairly_core.approvals.service import ApprovalRequest, create_approval
from pairly_core.approvals.sla import escalate_overdue
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.errors import BadRequestError
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, build_digests, deliver_org
from pairly_core.notifications.email import FakeEmailTransport
from pairly_core.notifications.service import (
    Recipient,
    SensitiveTemplateError,
    notify,
    record_sensitive_refusal,
    sensitive_message_payload,
)
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.notifications.unsubscribe import (
    MAX_AGE_SECONDS,
    UnsubscribeClaim,
    make_token,
    read_token,
)
from pairly_core.signed_links import hash_token
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_core.workflow.spec import ApprovalType
from pairly_worker.notification_jobs import deliver_notifications, send_digests

from .conftest import DbUrls
from .notify_support import (
    API_URL,
    LINK_SECRET,
    WEB_URL,
    NotifyApi,
    hooked_runtime,
    notifications,
    notify_api,
    notify_settings,
    text_of,
)
from .wf_support import (
    PERMISSION,
    REGISTRY,
    World,
    audit_actions,
    decide_as,
    org_session,
    other_world,
    owner_sql,
    runtime,
    start,
    wf_engine,
    world,
)

__all__ = ["notify_api", "other_world", "runtime", "wf_engine", "world"]  # fixtures

LINK_RE = re.compile(r"/approvals/link#([A-Za-z0-9_-]{43})")

APPROVAL_VARS: dict[str, Any] = {
    "title": "Expense 120",
    "step": "manager",
    "due_at": datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
    "inbox_url": f"{WEB_URL}/approvals",
}


def _tokens(content: dict[str, Any]) -> list[str]:
    """Distinct link tokens in a message (text and HTML carry the same ones)."""
    return list(dict.fromkeys(LINK_RE.findall(text_of(content))))


async def _set_prefs(owner: AsyncEngine, world: World, name: str, **values: Any) -> None:
    row = {
        "locale": "en",
        "timezone": "UTC",
        "channels": "{}",
        "digest": "{}",
        "snooze_until": None,
        "quiet_start": None,
        "quiet_end": None,
    } | values
    await owner_sql(
        owner,
        world.org_id,
        "INSERT INTO notification_preferences (org_id, user_id, locale, timezone, channels, "
        "digest, snooze_until, quiet_start, quiet_end) VALUES (:o, :u, :locale, :timezone, "
        "CAST(:channels AS jsonb), CAST(:digest AS jsonb), :snooze_until, :quiet_start, "
        ":quiet_end)",
        {"o": world.org_id, "u": world.id(name), **row},
    )


def _transports(settings: NotificationSettings, email: FakeEmailTransport) -> Transports:
    return Transports(settings=settings, email=email, slack=FakeSlackTransport())


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


async def _notify(maker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, **kwargs: Any) -> Any:
    async with maker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            return await notify(session, **kwargs)


async def _in_app(
    maker: async_sessionmaker[AsyncSession], world: World, name: str, title: str
) -> uuid.UUID:
    results = await _notify(
        maker,
        world.org_id,
        recipient=Recipient.user(world.id(name)),
        template_key="core.approval.requested",
        variables=APPROVAL_VARS | {"title": title},
        dedupe_key=f"test:{uuid.uuid4().hex}",
        channels=["in_app"],
    )
    assert results[0].status == "sent", results
    return uuid.UUID(str(results[0].notification_id))


# ------------------------------------------------------------------ approval messages


async def test_approval_emails_carry_signed_links_only_when_sent_now(
    world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine, migrated: DbUrls
) -> None:
    now = datetime.now(UTC)
    minute = now.hour * 60 + now.minute
    # appr1: defaults. appr2: approvals and SLA mail in the daily digest. dual: snoozed.
    # clerk_approver: inside quiet hours right now.
    await _set_prefs(
        owner_engine, world, "appr2", digest=json.dumps({"approvals": "daily", "sla": "daily"})
    )
    await _set_prefs(owner_engine, world, "dual", snooze_until=now + timedelta(hours=2))
    await _set_prefs(
        owner_engine,
        world,
        "clerk_approver",
        quiet_start=(minute - 60) % 1440,
        quiet_end=(minute + 60) % 1440,
    )
    settings = notify_settings()
    runtime = hooked_runtime(wf_engine, migrated, settings)
    run = await start(runtime, world)
    approval_id = run.pending_approval_id
    maker = runtime.sessionmaker

    emails = {
        row.recipient_user_id: (row, content)
        for row, content in await notifications(
            maker, world.org_id, channel="email", template_key="core.approval.requested"
        )
    }
    assert set(emails) == {world.id(n) for n in ("appr1", "appr2", "dual", "clerk_approver")}

    # Sent now: signed approve and reject links to the web confirmation page.
    row, content = emails[world.id("appr1")]
    assert (row.status, row.reason, row.urgency) == ("queued", None, "normal")
    approve, reject = _tokens(content)
    assert f"{WEB_URL}/approvals/link#{approve}" in content["text"]
    links = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT token_hash, user_id, action, channel, approval_id FROM approval_links "
        "WHERE token_hash IN (:a, :r) ORDER BY action",
        {"a": hash_token(approve), "r": hash_token(reject)},
    )
    assert [(r.user_id, r.action, r.channel, r.approval_id) for r in links] == [
        (world.id("appr1"), "approve", "email_link", approval_id),
        (world.id("appr1"), "reject", "email_link", approval_id),
    ]

    # Held (digest, snooze, quiet hours): the inbox link only, never a link token.
    held = {"appr2": ("digest", "digest"), "dual": ("queued", "snoozed")}
    held["clerk_approver"] = ("queued", "quiet_hours")
    for name, (status, reason) in held.items():
        row, content = emails[world.id(name)]
        assert (row.status, row.reason) == (status, reason), name
        assert _tokens(content) == [], name
        assert "/approvals/link" not in text_of(content), name
        assert content["link"] == f"{WEB_URL}/approvals/{approval_id}"
        if status == "queued":
            assert row.next_attempt_at is not None
            assert row.next_attempt_at > now
    issued_to = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT DISTINCT user_id FROM approval_links WHERE approval_id = :a",
        {"a": approval_id},
    )
    assert {r.user_id for r in issued_to} == {world.id("appr1")}

    # Slack is not linked for anybody: suppressed, no Slack tokens. In-app: everyone.
    slack = await notifications(maker, world.org_id, channel="slack")
    assert {(r.status, r.reason) for r, _ in slack} == {("suppressed", "slack_not_linked")}
    in_app = await notifications(maker, world.org_id, channel="in_app")
    assert {r.recipient_user_id for r, _ in in_app} == set(emails)
    assert {r.status for r, _ in in_app} == {"sent"}

    # SLA breach: urgent, so it skips the digest, the snooze and the quiet hours, and
    # carries fresh links for everyone who can still decide.
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE approval_steps SET warn_at = now() - interval '3 hours', "
        "due_at = now() - interval '1 hour' WHERE approval_id = :a",
        {"a": approval_id},
    )
    async with org_session(runtime, world.org_id) as session, session.begin():
        escalated = await escalate_overdue(session, REGISTRY, hooks=runtime.hooks)
    assert [e.approval_id for e in escalated] == [approval_id]
    urgent = {
        row.recipient_user_id: (row, content)
        for row, content in await notifications(
            maker, world.org_id, channel="email", template_key="core.approval.escalated"
        )
    }
    assert {world.id(n) for n in ("appr2", "dual", "clerk_approver", "fc1", "fc2")} <= set(urgent)
    for name in ("appr2", "dual", "clerk_approver"):
        row, content = urgent[world.id(name)]
        assert (row.status, row.reason, row.urgency) == ("queued", None, "urgent"), name
        assert row.next_attempt_at is not None
        # Due now by the database clock (the Postgres container may run slightly ahead).
        assert row.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=5)
        assert len(_tokens(content)) == 2, name
    assert "core.notification.held_for_digest" in await audit_actions(owner_engine, world.org_id)


# ------------------------------------------------------------------ preferences


async def test_preferences_api(
    notify_api: NotifyApi, world: World, owner_engine: AsyncEngine
) -> None:
    api = notify_api
    url = "/v1/notifications/preferences"
    defaults = await api.client.get(url, headers=api.headers(world, "appr1"))
    assert defaults.status_code == 200, defaults.text
    body = defaults.json()
    assert body["channels"]["approvals"] == ["email", "in_app", "slack"]
    assert body["digest"]["updates"] == "daily"
    assert "receipts" not in body["categories"]  # fixed, not configurable

    wanted = {
        "locale": "ar",
        "timezone": "Asia/Dubai",
        "channels": {"reminders": ["in_app"], "updates": []},
        "digest": {"approvals": "daily"},
        "quiet_start": 22 * 60,
        "quiet_end": 7 * 60,
    }
    saved = await api.client.put(url, json=wanted, headers=api.headers(world, "appr1"))
    assert saved.status_code == 200, saved.text
    again = (await api.client.get(url, headers=api.headers(world, "appr1"))).json()
    assert again == saved.json()
    assert (again["locale"], again["timezone"], again["quiet_start"]) == ("ar", "Asia/Dubai", 1320)
    assert again["channels"]["reminders"] == ["in_app"]
    assert again["channels"]["updates"] == []
    assert again["digest"]["approvals"] == "daily"
    # Only the caller's own row: another person still has the defaults.
    other = (await api.client.get(url, headers=api.headers(world, "appr2"))).json()
    assert other["digest"]["approvals"] == "realtime"

    for bad in (
        {"channels": {"receipts": ["email"]}},  # fixed category
        {"channels": {"nonsense": ["email"]}},
        {"digest": {"sla_typo": "daily"}},
        {"timezone": "Mars/Olympus_Mons"},
        {"quiet_start": 60},  # no end
        {"quiet_start": 60, "quiet_end": 60},
        {"snooze_until": "2026-10-08T09:00:00"},  # no time zone
    ):
        response = await api.client.put(url, json=bad, headers=api.headers(world, "appr1"))
        assert response.status_code == 400, (bad, response.text)
    unknown_channel = await api.client.put(
        url, json={"channels": {"approvals": ["fax"]}}, headers=api.headers(world, "appr1")
    )
    assert unknown_channel.status_code == 422
    assert (await api.client.get(url)).status_code == 401
    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("core.notification.preferences_changed") == 1


# ------------------------------------------------------------------ digests and unsubscribe


async def test_one_bad_item_never_blocks_the_organisations_digests(
    notify_api: NotifyApi, world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    api = notify_api
    maker = _maker(wf_engine)
    held: dict[str, uuid.UUID] = {}
    for name in ("appr1", "appr2"):
        saved = await api.client.put(
            "/v1/notifications/preferences",
            json={"digest": {"approvals": "daily"}},
            headers=api.headers(world, name),
        )
        assert saved.status_code == 200, saved.text
        (row,) = await _notify(
            maker,
            world.org_id,
            recipient=Recipient.user(world.id(name)),
            template_key="core.approval.requested",
            variables=APPROVAL_VARS | {"title": f"Invoice for {name}"},
            dedupe_key=f"digest-savepoint:{name}",
            channels=["email"],
        )
        assert row.status == "digest"
        held[name] = row.notification_id  # type: ignore[assignment]
    # appr1's held item can no longer be decrypted (a corrupted or foreign ciphertext).
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE notifications SET content = 'v1:not-a-valid-ciphertext' WHERE id = :i",
        {"i": held["appr1"]},
    )

    async with maker() as session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            stats = await build_digests(session)
    assert (stats.digests, stats.failed) == (1, 1)
    digests = await notifications(maker, world.org_id, template_key="core.digest.daily")
    assert [r.recipient_user_id for r, _ in digests] == [world.id("appr2")]
    assert "Invoice for appr2" in digests[0][1]["text"]
    status = {
        r.id: r.status
        for r in await owner_sql(
            owner_engine,
            world.org_id,
            "SELECT id, status FROM notifications WHERE id IN (:a, :b)",
            {"a": held["appr1"], "b": held["appr2"]},
        )
    }
    # The bad person's item stays held (tried again next time); the other one went out.
    assert status == {held["appr1"]: "digest", held["appr2"]: "sent"}


async def test_digest_job_and_one_click_unsubscribe(
    notify_api: NotifyApi, world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    api = notify_api
    maker = _maker(wf_engine)
    saved = await api.client.put(
        "/v1/notifications/preferences",
        json={"digest": {"approvals": "daily"}},
        headers=api.headers(world, "appr1"),
    )
    assert saved.status_code == 200, saved.text
    for title in ("Invoice A", "Invoice B"):
        (held,) = await _notify(
            maker,
            world.org_id,
            recipient=Recipient.user(world.id("appr1")),
            template_key="core.approval.requested",
            variables=APPROVAL_VARS | {"title": title},
            dedupe_key=f"digest-test:{title}",
            channels=["email"],
        )
        assert (held.status, held.reason) == ("digest", "digest")

    # The worker job builds one digest per person and day.
    summary = await send_digests({"sessionmaker": maker})
    assert summary["digests"] >= 1
    (digest, digest_content) = (
        await notifications(maker, world.org_id, template_key="core.digest.daily")
    )[0]
    assert (digest.status, digest.recipient_user_id) == ("queued", world.id("appr1"))
    assert "Invoice A" in digest_content["text"]
    assert "Invoice B" in digest_content["text"]
    items = await notifications(maker, world.org_id, template_key="core.approval.requested")
    assert {(r.status, r.reason, r.digest_id) for r, _ in items} == {("sent", "digest", digest.id)}
    # Nothing is held any more: running again builds nothing new.
    async with maker() as session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            assert (await build_digests(session)).digests == 0

    # Delivery: the digest is not transactional, so it carries List-Unsubscribe.
    fake = FakeEmailTransport()
    stats = await deliver_org(maker, world.org_id, _transports(api.settings, fake))
    assert stats.sent == 1
    (email,) = fake.sent
    assert email.to.endswith("@example.test")
    assert email.list_unsubscribe is not None
    prefix = f"{API_URL}/v1/notifications/unsubscribe?token="
    assert email.list_unsubscribe.startswith(prefix)
    token = email.list_unsubscribe.removeprefix(prefix)
    assert read_token(LINK_SECRET, token) == UnsubscribeClaim(
        world.org_id, world.id("appr1"), "digest"
    )

    # One click (no sign-in): email off for that category only.
    response = await api.client.post(f"/v1/notifications/unsubscribe?token={token}")
    assert response.status_code == 204
    prefs = (
        await api.client.get("/v1/notifications/preferences", headers=api.headers(world, "appr1"))
    ).json()
    assert "email" not in prefs["channels"]["digest"]
    assert "email" in prefs["channels"]["approvals"]
    assert prefs["digest"]["approvals"] == "daily"  # the rest is kept
    # Idempotent: a second click changes nothing and still answers 204.
    assert (
        await api.client.post(f"/v1/notifications/unsubscribe?token={token}")
    ).status_code == 204
    # The worker delivery job finds nothing more to send for this organisation.
    worker_fake = FakeEmailTransport()
    await deliver_notifications(
        {"sessionmaker": maker, "transports": _transports(api.settings, worker_fake)}
    )
    assert all(m.to != email.to for m in worker_fake.sent)
    actions = await audit_actions(owner_engine, world.org_id)
    assert "core.notification.sent" in actions
    assert actions.count("core.notification.unsubscribed") == 2


async def test_unsubscribe_refuses_tampered_expired_and_foreign_tokens(
    notify_api: NotifyApi, world: World, other_world: World, owner_engine: AsyncEngine
) -> None:
    api = notify_api
    me = world.id("appr1")

    async def prefs() -> dict[str, Any]:
        response = await api.client.get(
            "/v1/notifications/preferences", headers=api.headers(world, "appr1")
        )
        assert response.status_code == 200
        return dict(response.json()["channels"])

    async def click(token: str) -> int:
        response = await api.client.post("/v1/notifications/unsubscribe", params={"token": token})
        return response.status_code

    before = await prefs()
    valid = make_token(LINK_SECRET, UnsubscribeClaim(world.org_id, me, "updates"))
    parts = valid.split(".")
    tampered = ".".join([*parts[:3], "approvals", *parts[4:]])
    expired = make_token(
        LINK_SECRET,
        UnsubscribeClaim(world.org_id, me, "sla"),
        now=time.time() - MAX_AGE_SECONDS - 60,
    )
    wrong_key = make_token("another-secret-" + "y" * 30, UnsubscribeClaim(world.org_id, me, "sla"))
    # A person of another organisation, under this organisation: not a member.
    foreign = make_token(
        LINK_SECRET, UnsubscribeClaim(world.org_id, other_world.id("appr1"), "updates")
    )
    fixed = make_token(LINK_SECRET, UnsubscribeClaim(world.org_id, me, "receipts"))
    # Every refusal answers like a success: the endpoint reveals nothing.
    for token in (tampered, expired, wrong_key, foreign, fixed, "v1." + "a" * 40):
        assert await click(token) == 204
    assert await prefs() == before
    assert (await api.client.post("/v1/notifications/unsubscribe")).status_code == 422
    assert "core.notification.unsubscribed" not in await audit_actions(owner_engine, world.org_id)
    other_rows = await owner_sql(
        owner_engine, world.org_id, "SELECT user_id FROM notification_preferences"
    )
    assert other_world.id("appr1") not in {r.user_id for r in other_rows}

    assert await click(valid) == 204
    after = await prefs()
    assert "email" not in after["updates"]
    assert "in_app" in after["updates"]
    assert after["sla"] == before["sla"]
    assert after["approvals"] == before["approvals"]
    assert (await audit_actions(owner_engine, world.org_id)).count(
        "core.notification.unsubscribed"
    ) == 1


async def test_unsubscribe_landing_tells_an_expired_link_apart(
    notify_api: NotifyApi, world: World, owner_engine: AsyncEngine
) -> None:
    api = notify_api
    me = world.id("appr1")
    valid = make_token(LINK_SECRET, UnsubscribeClaim(world.org_id, me, "updates"))
    expired = make_token(
        LINK_SECRET,
        UnsubscribeClaim(world.org_id, me, "updates"),
        now=time.time() - MAX_AGE_SECONDS - 60,
    )
    forged = make_token("another-secret-" + "y" * 30, UnsubscribeClaim(world.org_id, me, "sla"))
    fixed = make_token(LINK_SECRET, UnsubscribeClaim(world.org_id, me, "receipts"))

    async def land(token: str) -> dict[str, Any]:
        response = await api.client.get("/v1/notifications/unsubscribe", params={"token": token})
        assert response.status_code == 200, response.text
        return dict(response.json())

    manage = f"{WEB_URL}/settings"
    assert await land(valid) == {"state": "valid", "category": "updates", "manage_url": manage}
    # Expired: never a silent failure, the person is sent to sign in and manage.
    assert await land(expired) == {"state": "expired", "category": None, "manage_url": manage}
    for token in (forged, fixed, "v1." + "a" * 40):
        assert (await land(token))["state"] == "invalid"
    # A browser goes to the web page, the token in the fragment only.
    page = await api.client.get(
        "/v1/notifications/unsubscribe",
        params={"token": expired},
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert page.status_code == 303
    assert page.headers["location"].startswith(f"{WEB_URL}/unsubscribe#token=")
    assert "?" not in page.headers["location"]
    # Opening the landing changes nothing (mail scanners open links).
    assert "core.notification.unsubscribed" not in await audit_actions(owner_engine, world.org_id)
    # The one-click POST keeps its uniform 204 for an expired link, and changes nothing.
    response = await api.client.post("/v1/notifications/unsubscribe", params={"token": expired})
    assert response.status_code == 204
    assert "core.notification.unsubscribed" not in await audit_actions(owner_engine, world.org_id)


# ------------------------------------------------------------------ delivery


async def test_delivery_retries_are_idempotent(
    world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    maker = _maker(wf_engine)
    settings = notify_settings(delivery_max_attempts=3, delivery_backoff_seconds=60)
    receipt: dict[str, Any] = {
        "recipient": Recipient.external("Vendor@Example.test"),
        "template_key": "core.ingest.receipt",
        "variables": {
            "reference": "REF-0011223344",
            "received_at": datetime(2026, 10, 7, 9, 30, tzinfo=UTC),
            "document_count": 1,
        },
        "channels": ["email"],
    }
    (first,) = await _notify(maker, world.org_id, dedupe_key="receipt:retry-1", **receipt)
    (again,) = await _notify(maker, world.org_id, dedupe_key="receipt:retry-1", **receipt)
    assert first.status == "queued"
    assert not first.replayed
    assert again.replayed
    assert again.notification_id == first.notification_id
    assert len(await notifications(maker, world.org_id, dedupe_key="receipt:retry-1:email")) == 1

    fake = FakeEmailTransport(fail_next=1)
    stats = await deliver_org(maker, world.org_id, _transports(settings, fake))
    assert (stats.sent, stats.retried, stats.failed) == (0, 1, 0)
    ((row, _),) = await notifications(maker, world.org_id, id=first.notification_id)
    assert (row.status, row.attempts, row.reason) == ("queued", 1, "fake_failure")
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > datetime.now(UTC)
    # Not due yet: nothing is sent early.
    stats = await deliver_org(maker, world.org_id, _transports(settings, fake))
    assert (stats.sent, stats.retried, fake.attempts) == (0, 0, 1)

    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE notifications SET next_attempt_at = now() - interval '1 second' WHERE id = :i",
        {"i": first.notification_id},
    )
    stats = await deliver_org(maker, world.org_id, _transports(settings, fake))
    assert stats.sent == 1
    (sent,) = fake.sent
    # The retry reuses the Message-ID and the provider idempotency key.
    assert sent.message_id == f"{first.notification_id}@pairly.localhost"
    assert sent.idempotency_key == f"{world.org_id}:receipt:retry-1:email"
    assert sent.to == "Vendor@Example.test"
    # Sent once: more runs and a repeated notify call send nothing.
    await deliver_org(maker, world.org_id, _transports(settings, fake))
    (replayed,) = await _notify(maker, world.org_id, dedupe_key="receipt:retry-1", **receipt)
    assert (replayed.replayed, replayed.status) == (True, "sent")
    await deliver_org(maker, world.org_id, _transports(settings, fake))
    assert len(fake.sent) == 1

    # A permanent error fails at once; transient errors give up after the last attempt.
    (perm,) = await _notify(maker, world.org_id, dedupe_key="receipt:perm", **receipt)
    stats = await deliver_org(
        maker,
        world.org_id,
        _transports(settings, FakeEmailTransport(fail_next=1, fail_permanent=True)),
    )
    assert stats.failed == 1
    ((row, _),) = await notifications(maker, world.org_id, id=perm.notification_id)
    assert (row.status, row.attempts, row.next_attempt_at) == ("failed", 1, None)

    (flaky,) = await _notify(maker, world.org_id, dedupe_key="receipt:flaky", **receipt)
    always = FakeEmailTransport(fail_next=10)
    for _ in range(settings.delivery_max_attempts):
        await deliver_org(maker, world.org_id, _transports(settings, always))
        await owner_sql(
            owner_engine,
            world.org_id,
            "UPDATE notifications SET next_attempt_at = now() - interval '1 second' "
            "WHERE id = :i AND status = 'queued'",
            {"i": flaky.notification_id},
        )
    ((row, _),) = await notifications(maker, world.org_id, id=flaky.notification_id)
    assert (row.status, row.attempts) == ("failed", settings.delivery_max_attempts)
    actions = await audit_actions(owner_engine, world.org_id)
    for action in (
        "core.notification.queued",
        "core.notification.retry_scheduled",
        "core.notification.sent",
        "core.notification.failed",
    ):
        assert action in actions, action


# ------------------------------------------------------------------ sensitive templates

MESSAGE_RULES = """\
schema: pairly.approval_rules/v1
approval_type: testwf.message
sla_hours: 24
warn_hours: 4
chains:
  - name: review
    steps:
      - {name: reviewer, mode: any_of, roles: [approver]}
"""
MESSAGE_TYPE = ApprovalType("testwf.message", PERMISSION, MESSAGE_RULES, "Candidate message")
CANDIDATE = Recipient.external("sam.example@example.test")
REJECTION: dict[str, Any] = {
    "candidate_name": "Sam Example",
    "job_title": "Accounts Payable Specialist",
    "company": "Example Trading LLC",
    "reason": "We chose a candidate with more payables experience.",
}


async def _send_rejection(
    runtime: WorkflowRuntime,
    world: World,
    *,
    approval_id: uuid.UUID | None,
    dedupe_key: str,
    variables: dict[str, Any] = REJECTION,
) -> Any:
    async with org_session(runtime, world.org_id) as session, session.begin():
        return await notify(
            session,
            recipient=CANDIDATE,
            template_key="hire.candidate.rejection",
            variables=variables,
            dedupe_key=dedupe_key,
            approval_id=approval_id,
            registry=REGISTRY,
            workflow="hire",
        )


async def _message_approval(runtime: WorkflowRuntime, world: World, run_id: uuid.UUID) -> Any:
    async with org_session(runtime, world.org_id) as session, session.begin():
        payload = await sensitive_message_payload(
            session,
            recipient=CANDIDATE,
            template_key="hire.candidate.rejection",
            variables=REJECTION,
        )
        return await create_approval(
            session,
            REGISTRY,
            ApprovalRequest(
                run_id=run_id,
                step_key=f"message-{uuid.uuid4().hex[:8]}",
                approval_type=MESSAGE_TYPE,
                title="Rejection email to Sam Example",
                payload=payload,
            ),
        )


async def test_sensitive_template_needs_its_own_approved_approval(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    approval = await _message_approval(runtime, world, run.id)
    assert approval.payload["text"].startswith("Dear Sam Example")

    async def refused(approval_id: uuid.UUID | None, **kw: Any) -> str:
        with pytest.raises(SensitiveTemplateError) as caught:
            await _send_rejection(
                runtime, world, approval_id=approval_id, dedupe_key=f"rej:{uuid.uuid4()}", **kw
            )
        return caught.value.reason

    assert await refused(None) == "approval_missing"
    assert await refused(uuid.uuid4()) == "approval_not_found"
    assert await refused(approval.id) == "approval_not_approved"  # still pending

    await decide_as(runtime, world, "appr1", approval.id)
    # Approved, but the message differs from what the approver saw.
    changed = REJECTION | {"reason": "A different reason nobody approved."}
    assert await refused(approval.id, variables=changed) == "payload_hash_mismatch"
    # A refusal sends nothing and leaves no row.
    assert await notifications(runtime.sessionmaker, world.org_id, approval_id=approval.id) == []

    (sent,) = await _send_rejection(runtime, world, approval_id=approval.id, dedupe_key="rej:ok")
    assert (sent.status, sent.channel) == ("queued", "email")
    ((row, content),) = await notifications(
        runtime.sessionmaker, world.org_id, approval_id=approval.id
    )
    assert row.content_hash == approval.payload_hash
    assert content["text"] == approval.payload["text"]
    # A retry of the same message is a replay; any other message is refused.
    (replay,) = await _send_rejection(runtime, world, approval_id=approval.id, dedupe_key="rej:ok")
    assert replay.replayed
    assert replay.notification_id == sent.notification_id
    assert await refused(approval.id) == "approval_already_used"

    # A normal template never takes an approval id.
    with pytest.raises(BadRequestError):
        async with org_session(runtime, world.org_id) as session, session.begin():
            await notify(
                session,
                recipient=CANDIDATE,
                template_key="core.ingest.receipt",
                variables={
                    "reference": "REF-1",
                    "received_at": datetime.now(UTC),
                    "document_count": 0,
                },
                dedupe_key="receipt:with-approval",
                approval_id=approval.id,
            )

    async with org_session(runtime, world.org_id) as session, session.begin():
        await record_sensitive_refusal(
            session,
            template_key="hire.candidate.rejection",
            approval_id=approval.id,
            reason="approval_already_used",
        )
    assert "core.notification.sensitive_refused" in await audit_actions(owner_engine, world.org_id)


async def test_rejected_approval_never_sends_the_sensitive_message(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world)
    approval = await _message_approval(runtime, world, run.id)
    await decide_as(runtime, world, "appr1", approval.id, Decision.REJECT, reason="Tone is off")
    with pytest.raises(SensitiveTemplateError) as caught:
        await _send_rejection(runtime, world, approval_id=approval.id, dedupe_key="rej:rejected")
    assert caught.value.reason == "approval_not_approved"


# ------------------------------------------------------------------ in-app feed


async def test_in_app_feed_is_scoped_to_the_person_and_organisation(
    notify_api: NotifyApi, world: World, other_world: World
) -> None:
    api = notify_api
    maker = api.sessionmaker
    mine = [await _in_app(maker, world, "appr1", f"Mine {i}") for i in range(2)]
    colleague = await _in_app(maker, world, "appr2", "Colleague")
    foreign = await _in_app(maker, other_world, "appr1", "Other organisation")

    page = await api.client.get("/v1/notifications", headers=api.headers(world, "appr1"))
    assert page.status_code == 200, page.text
    body = page.json()
    assert {i["id"] for i in body["items"]} == {str(i) for i in mine}
    assert body["unread"] == 2
    assert body["items"][0]["title"] == "Approval needed: Mine 1"  # newest first
    assert body["items"][0]["link"] is None

    for other in (colleague, foreign):
        response = await api.client.post(
            f"/v1/notifications/{other}/read", headers=api.headers(world, "appr1")
        )
        assert response.status_code == 404, response.text
    # Not a member of the other organisation.
    response = await api.client.get(
        "/v1/notifications", headers=api.headers(world, "appr1", org=other_world.org_id)
    )
    assert response.status_code == 403

    read = await api.client.post(
        f"/v1/notifications/{mine[0]}/read", headers=api.headers(world, "appr1")
    )
    assert read.status_code == 200
    assert read.json()["read_at"] is not None
    unread = await api.client.get(
        "/v1/notifications?unread=true", headers=api.headers(world, "appr1")
    )
    assert [i["id"] for i in unread.json()["items"]] == [str(mine[1])]
    marked = await api.client.post(
        "/v1/notifications/read-all", headers=api.headers(world, "appr1")
    )
    assert marked.json() == {"marked": 1}
    # Other people's items are untouched.
    for who, name in ((world, "appr2"), (other_world, "appr1")):
        response = await api.client.get("/v1/notifications", headers=api.headers(who, name))
        assert response.json()["unread"] == 1


async def test_event_stream_needs_auth_and_streams_only_the_callers_items(
    notify_api: NotifyApi, world: World, other_world: World
) -> None:
    api = notify_api
    maker = api.sessionmaker
    url = "/v1/notifications/stream"
    assert (await api.client.get(url)).status_code == 401
    assert (
        await api.client.get(url, headers=api.headers(world, "appr1", org=other_world.org_id))
    ).status_code == 403

    first = await _in_app(maker, world, "appr1", "First")
    second = await _in_app(maker, world, "appr1", "Second")
    colleague = await _in_app(maker, world, "appr2", "Colleague")
    foreign = await _in_app(maker, other_world, "appr1", "Foreign")

    # Resume after ``first``: only the caller's later items.
    resumed = await api.client.get(
        url, headers=api.headers(world, "appr1") | {"Last-Event-ID": str(first)}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.headers["content-type"].startswith("text/event-stream")
    assert f"id: {second}" in resumed.text
    for absent in (first, colleague, foreign):
        assert str(absent) not in resumed.text
    assert "event: end" in resumed.text

    # Live: items created while the stream is open arrive; other people's never do.
    task = asyncio.create_task(api.client.get(url, headers=api.headers(world, "appr1")))
    await asyncio.sleep(0.3)
    live = await _in_app(maker, world, "appr1", "Live")
    live_colleague = await _in_app(maker, world, "appr2", "Live colleague")
    live_foreign = await _in_app(maker, other_world, "appr1", "Live foreign")
    streamed = await task
    assert streamed.status_code == 200
    assert f"id: {live}" in streamed.text
    assert '"title":"Approval needed: Live"' in streamed.text
    for absent in (second, live_colleague, live_foreign):
        assert str(absent) not in streamed.text

    bad = await api.client.get(
        url, headers=api.headers(world, "appr1") | {"Last-Event-ID": "not-a-uuid"}
    )
    assert bad.status_code == 400


# ------------------------------------------------------------------ templates API


def _draft(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": "testwf.notice.hello",
        "channel": "email",
        "locale": "en",
        "category": "updates",
        "subject": "Hello {{ name }}",
        "body": "Hello {{ name }}, your report is ready.",
        "variables": {"name": {"type": "string", "required": True, "sample": "Sam"}},
    }
    return body | overrides


async def test_templates_api(
    notify_api: NotifyApi,
    world: World,
    other_world: World,
    owner_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    api = notify_api
    base = "/v1/notification-templates"
    admin = api.headers(world, "admin")

    first = await api.client.post(base, json=_draft(), headers=admin)
    assert first.status_code == 201, first.text
    saved = first.json()
    assert (saved["template"]["version"], saved["template"]["system"]) == (1, False)
    assert "missing_ar" in [i["code"] for i in saved["issues"]]
    second = await api.client.post(
        base, json=_draft(body="Hi {{ name }}, the report is ready."), headers=admin
    )
    assert second.json()["template"]["version"] == 2
    versions = await api.client.get(f"{base}/testwf.notice.hello/versions", headers=admin)
    assert [v["version"] for v in versions.json()] == [2, 1]

    # Step-up and permissions: saving needs core.template.manage and a TOTP sign-in.
    assert (
        await api.client.post(base, json=_draft(), headers=api.headers(world, "admin", otp=False))
    ).status_code == 403
    assert (
        await api.client.post(base, json=_draft(), headers=api.headers(world, "clerk"))
    ).status_code == 403
    assert (await api.client.get(base, headers=api.headers(world, "appr1"))).status_code == 403
    # Tenant isolation: the other organisation sees the system defaults, not our version.
    theirs = await api.client.get(base, headers=api.headers(other_world, "admin"))
    assert "testwf.notice.hello" not in {t["key"] for t in theirs.json()}
    ours = await api.client.get(base, headers=api.headers(world, "clerk"))
    assert "testwf.notice.hello" in {t["key"] for t in ours.json()}
    assert "hire.candidate.rejection" in {t["key"] for t in ours.json()}

    # Lint rejections answer 400 with every issue.
    for draft, code in (
        (_draft(body="Hello {{ name }}, ready — enjoy."), "em_dash"),
        (_draft(body="Hello {{ name }} and {{ boss }}."), "undeclared_variable"),
        (_draft(subject=None), "subject_required"),
        (_draft(body="{{ name.__class__ }}"), "private_name"),
        (_draft(key="core.approval.requested", category="sla"), "category_fixed"),
    ):
        response = await api.client.post(base, json=draft, headers=admin)
        assert response.status_code == 400, (code, response.text)
        problem = response.json()
        assert problem["code"] == "template_invalid"
        assert code in [i["code"] for i in problem["issues"]], (code, problem["issues"])

    # The approval flag of a sensitive template cannot be cleared by a new version.
    sensitive = await api.client.post(
        base,
        json={
            "key": "hire.candidate.rejection",
            "channel": "email",
            "locale": "en",
            "subject": "About {{ job_title }}",
            "body": "Dear {{ candidate_name }}, thank you. {{ company }}",
            "variables": {
                "candidate_name": {"type": "string"},
                "job_title": {"type": "string"},
                "company": {"type": "string"},
            },
            "requires_approval": False,
        },
        headers=admin,
    )
    assert sensitive.status_code == 201, sensitive.text
    assert sensitive.json()["template"]["requires_approval"] is True
    assert "requires_approval_kept" in [i["code"] for i in sensitive.json()["issues"]]

    # Preview: a draft and a saved version, never sent.
    previewed = await api.client.post(
        f"{base}/preview",
        json={
            "channel": "email",
            "locale": "ar",
            "subject": "مرحبا {{ name }}",
            "body": "مرحبا {{ name }}",
            "variables": {"name": {"type": "string", "sample": "سام"}},
            "values": {"name": "ليلى"},
        },
        headers=admin,
    )
    assert previewed.status_code == 200, previewed.text
    assert (previewed.json()["text"], previewed.json()["direction"]) == ("مرحبا ليلى", "rtl")
    stored = await api.client.post(
        f"{base}/preview", json={"template_id": first.json()["template"]["id"]}, headers=admin
    )
    assert stored.json()["subject"] == "Hello Sam"  # declared sample values
    assert (
        await api.client.post(f"{base}/preview", json={"channel": "email"}, headers=admin)
    ).status_code == 400
    assert await notifications(api.sessionmaker, world.org_id) == []

    # Versions are immutable. The application role has no UPDATE or DELETE grant. The
    # owner has no UPDATE or DELETE policy under forced RLS, so nothing matches; and with
    # RLS out of the way (a role that bypasses it) the trigger refuses.
    template_id = first.json()["template"]["id"]

    async def attempt(engine: AsyncEngine, statement: str, *, no_rls: bool = False) -> int | None:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.org_id', :o, true)"), {"o": str(world.org_id)}
                )
                if no_rls:
                    await conn.execute(text("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY"))
                result = await conn.execute(text(statement), {"i": template_id})
                return int(result.rowcount)
        except DBAPIError:
            return None

    for statement in (
        "UPDATE templates SET body = 'changed' WHERE id = :i",
        "DELETE FROM templates WHERE id = :i",
        "UPDATE templates SET body = 'changed' WHERE org_id IS NULL",
    ):
        assert await attempt(app_engine, statement) is None, statement
        assert await attempt(owner_engine, statement) in (None, 0), statement
        assert await attempt(owner_engine, statement, no_rls=True) is None, statement
    (row,) = await owner_sql(
        owner_engine, world.org_id, "SELECT body FROM templates WHERE id = :i", {"i": template_id}
    )
    assert row.body == "Hello {{ name }}, your report is ready."
    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("core.template.version_created") == 3

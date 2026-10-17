# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The first-deletion notice and the pause of a retention rule (``pairly_core.retention``).

* Notice: the run that first lists a due candidate sends the admins an urgent email and
  in-app notice (``core.retention.first_deletion``, linking the Retention settings page),
  records it on the rule, and never sends it again.
* A list made before any notice (a dry run, or an organisation from before the notice)
  is kept a whole preview after the notice: the notice comes before the first deletion.
* Pause (step-up, audited) blocks the run; resuming starts a new seven-day preview.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.notifications.defaults import RETENTION_FIRST_DELETION
from pairly_core.notifications.templating import (
    TemplateSource,
    parse_declarations,
    render_source,
)
from pairly_core.retention.service import run_org
from pairly_hire.retention import RULE_KEY

from .hire_support import (
    HireApi,
    HireWorld,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .test_retention_deletion import _apply, _close, _ended_preview, _erased, _run

__all__ = ["hire_api"]  # fixtures

BASE = "/v1/retention"
TEMPLATE = "core.retention.first_deletion"


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


async def _notices(owner: AsyncEngine, world: HireWorld) -> list[tuple[str, str, str]]:
    rows = await owner_rows(
        owner,
        world.org_id,
        "SELECT recipient_user_id::text AS u, channel, urgency, category FROM notifications "
        "WHERE template_key = :k ORDER BY channel, recipient_user_id",
        {"k": TEMPLATE},
    )
    for r in rows:
        assert r.category == "retention"
    return [(r.u, r.channel, r.urgency) for r in rows]


async def _rule(api: HireApi, world: HireWorld) -> dict[str, object]:
    settings = await api.call("GET", f"{BASE}/settings", world, "admin")
    assert settings.status_code == 200, settings.text
    (rule,) = [r for r in settings.json()["rules"] if r["key"] == RULE_KEY]
    return rule


def test_the_notice_names_the_count_the_date_and_the_choice() -> None:
    (email,) = [t for t in RETENTION_FIRST_DELETION if (t.channel, t.locale) == ("email", "en")]
    src = TemplateSource(
        email.channel,  # type: ignore[arg-type]
        email.locale,  # type: ignore[arg-type]
        email.subject,
        email.body,
        email.body_html,
        parse_declarations(email.variables),
    )
    out = render_source(
        src,
        {
            "count": 72,
            "subject_type": "hire.candidate",
            "deletes_on": datetime(2026, 10, 23, 9, tzinfo=UTC),
            "review_url": "https://app.pairly.example/settings/retention",
        },
    )
    assert out.text.startswith(
        "Retention will start deleting 72 closed candidate records on 23 Oct 2026, 09:00 (UTC). "
        "Review the list or pause."
    )
    assert "https://app.pairly.example/settings/retention" in out.text
    assert "—" not in out.text
    for t in RETENTION_FIRST_DELETION:
        assert t.category == "retention"
        assert {"count", "subject_type", "deletes_on"} <= set(t.variables)


async def test_the_admins_are_told_once_before_the_first_deletion(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    job = await open_job(api, world)
    first = await _close(owner_engine, world, await _apply(api, world, job, "notice-1"), 13)
    second = await _close(owner_engine, world, await _apply(api, world, job, "notice-2"), 14)
    assert (await _rule(api, world))["first_notice_at"] is None

    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 0
    notices = await _notices(owner_engine, world)
    admin = str(world.people["admin"][0])
    assert notices == [(admin, "email", "urgent"), (admin, "in_app", "urgent")]
    # Only the people who may change retention: no recruiter, no auditor.
    assert {u for u, _, _ in notices} == {admin}
    (control,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT notice_sent_at, notice_count, notice_deletes_on FROM retention_rule_controls "
        "WHERE rule = :r",
        {"r": RULE_KEY},
    )
    assert control.notice_count == 2
    schedule = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert min(datetime.fromisoformat(i["delete_after"]) for i in schedule) == (
        control.notice_deletes_on
    )
    assert control.notice_deletes_on >= control.notice_sent_at + timedelta(days=7)
    (audited,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM audit_events WHERE action = 'core.retention.first_notice_sent'",
    )
    assert audited.n == 1
    assert (await _rule(api, world))["first_notice_at"] is not None

    # Never again: not on the next run, not once deletion starts, not for a new subject.
    await _run(api.engine, registry, world, api.store)
    await _ended_preview(owner_engine, world)
    deleted, failed = await _run(api.engine, registry, world, api.store)
    assert (deleted[RULE_KEY], failed) == (2, 0)
    assert await _erased(owner_engine, world, first)
    assert await _erased(owner_engine, world, second)
    await _close(owner_engine, world, await _apply(api, world, job, "notice-3"), 15)
    await _run(api.engine, registry, world, api.store)
    assert await _notices(owner_engine, world) == notices


async def test_a_list_made_before_any_notice_waits_a_whole_preview_after_it(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    job = await open_job(api, world)
    cid = await _close(owner_engine, world, await _apply(api, world, job, "early"), 13)
    # A dry run lists without telling anyone; then its preview ends.
    await run_org(
        create_sessionmaker(api.engine),
        registry,
        world.org_id,
        run_id=uuid.uuid4(),
        store=api.store,
        delete_data=False,
    )
    assert await _notices(owner_engine, world) == []
    await _ended_preview(owner_engine, world)

    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 0  # told now, deleted no sooner than a week from now
    assert not await _erased(owner_engine, world, cid)
    assert len(await _notices(owner_engine, world)) == 2
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    later = datetime.fromisoformat(item["delete_after"]) - datetime.now(UTC)
    assert later > timedelta(days=6, hours=23)
    report = (await api.call("GET", f"{BASE}/report", world, "admin")).json()
    (row,) = [r for r in report["rules"] if r["rule"] == RULE_KEY]
    assert row["scheduled_for"] == item["delete_after"]


async def test_a_paused_rule_deletes_nothing_and_resuming_starts_a_new_preview(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    job = await open_job(api, world)
    cid = await _close(owner_engine, world, await _apply(api, world, job, "paused"), 13)
    await _run(api.engine, registry, world, api.store)  # listed, admins told

    # A second factor is needed, and only admins may pause.
    path = f"{BASE}/rules/{RULE_KEY}/pause"
    assert (await api.call("POST", path, world, "admin", otp=False)).status_code == 403
    assert (await api.call("POST", path, world, "auditor", otp=False)).status_code == 403
    assert (
        await api.call("POST", f"{BASE}/rules/hire.nope/pause", world, "admin")
    ).status_code == 404
    paused = await api.call("POST", path, world, "admin")
    assert paused.status_code == 200, paused.text
    (rule,) = [r for r in paused.json()["rules"] if r["key"] == RULE_KEY]
    assert rule["paused"] is True
    assert rule["paused_at"] is not None
    assert (await api.call("POST", path, world, "admin")).status_code == 409

    await _ended_preview(owner_engine, world)
    deleted, failed = await _run(api.engine, registry, world, api.store)
    assert (deleted[RULE_KEY], failed) == (0, 0)
    assert not await _erased(owner_engine, world, cid)
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert item["status"] == "scheduled"  # still on the list, just not deleted
    report = (await api.call("GET", f"{BASE}/report", world, "admin")).json()
    (row,) = [r for r in report["rules"] if r["rule"] == RULE_KEY]
    assert row["scheduled_for"] is None

    resumed = await api.call("POST", f"{BASE}/rules/{RULE_KEY}/resume", world, "admin")
    assert resumed.status_code == 200, resumed.text
    (rule,) = [r for r in resumed.json()["rules"] if r["key"] == RULE_KEY]
    assert rule["paused"] is False
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert datetime.fromisoformat(item["delete_after"]) - datetime.now(UTC) > timedelta(
        days=6, hours=23
    )
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 0  # the new preview runs first
    assert not await _erased(owner_engine, world, cid)
    await _ended_preview(owner_engine, world)
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 1
    assert await _erased(owner_engine, world, cid)
    assert (
        await api.call("POST", f"{BASE}/rules/{RULE_KEY}/resume", world, "admin")
    ).status_code == 409
    assert (
        await api.call("POST", f"{BASE}/rules/hire.checklist.documents/pause", world, "admin")
    ).status_code == 400  # a report rule has nothing to pause

    audit = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action, actor_id, payload FROM audit_events WHERE action IN "
        "('core.retention.rule_paused', 'core.retention.rule_resumed') ORDER BY seq",
    )
    assert [a.action for a in audit] == [
        "core.retention.rule_paused",
        "core.retention.rule_resumed",
    ]
    assert {a.actor_id for a in audit} == {str(world.people["admin"][0])}
    assert audit[0].payload["listed"] == 1
    assert audit[1].payload["relisted"] == 1

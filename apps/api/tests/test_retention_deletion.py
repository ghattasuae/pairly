# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention with automatic deletion (brief 6.9, ``pairly_core.retention``) on Postgres.

* Default: a candidate whose applications all ended (rejected or withdrawn) more than
  12 months ago is due; one rejected 11 months ago is not.
* Preview: the first run lists the due candidate for seven days ("scheduled for deletion
  on <date>") and deletes nothing; once the preview ended the next run erases them
  through the candidate erasure, and a search of every table and stored object finds
  nothing of them but the trail (the erasure search test); the deletion is a decision
  record with the system actor, audited.
* A legal hold blocks the deletion until released (and releasing starts a new preview).
* The daily cap: no more subjects a day than the organisation's cap.
* A rule switched off lists and deletes nothing.
* Fail closed: an erasure that fails half way leaves the candidate untouched and the
  error on the list; a store that fails keeps the subject ``erasing`` until a run
  removes the files.
* Technical rows are deleted only once they have been due for the whole preview.
* Tenant isolation: another organisation sees none of the list, the holds or the
  settings, and its run deletes nothing of this one.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.ingestion.storage import InMemoryObjectStore, ObjectStore, StorageError
from pairly_core.retention.rules import RetentionRegistry, RetentionRule, SubjectRule
from pairly_core.retention.service import run_org
from pairly_hire.retention import RULE_KEY, erase_due_candidate
from synth.hire.screening import contact_for

from .hire_support import (
    HireApi,
    HireWorld,
    cv_pdf,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .test_hire_erasure import (
    CANDIDATE,
    OTHER,
    _found,
    _gather,
    _haystack,
    _sentence,
    _shortlisted,
)
from .test_hire_screening import ScreenFactory, screen_factory, screening_world, spec

__all__ = ["hire_api", "screen_factory"]  # fixtures

BASE = "/v1/retention"


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture
async def screened(owner_engine: AsyncEngine) -> HireWorld:
    return await screening_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


async def _close(
    owner: AsyncEngine, world: HireWorld, reference: str, months_ago: int, status: str = "rejected"
) -> uuid.UUID:
    """End the application ``reference`` ``months_ago`` (rejected, or withdrawn with its
    stage move) and return its candidate."""
    if status == "rejected":
        await owner_rows(
            owner,
            world.org_id,
            "UPDATE hire_applications SET status = 'rejected', "
            "rejected_at = now() - make_interval(months => :m), "
            "retention_until = now() - make_interval(months => :m) + interval '12 months' "
            "WHERE reference = :r",
            {"m": months_ago, "r": reference},
        )
    else:
        await owner_rows(
            owner,
            world.org_id,
            "UPDATE hire_applications SET status = 'withdrawn', rejected_at = NULL, "
            "retention_until = NULL, updated_at = now() - make_interval(months => :m) "
            "WHERE reference = :r",
            {"m": months_ago, "r": reference},
        )
    ((candidate,),) = await owner_rows(
        owner, world.org_id, "SELECT candidate_id FROM hire_applications WHERE reference = :r",
        {"r": reference},
    )  # fmt: skip
    return uuid.UUID(str(candidate))


async def _ended_preview(owner: AsyncEngine, world: HireWorld) -> None:
    """As if the seven days of the preview had passed."""
    await owner_rows(
        owner,
        world.org_id,
        "UPDATE retention_schedule SET listed_at = now() - interval '8 days', "
        "delete_after = now() - interval '1 day' WHERE status = 'scheduled'",
    )


async def _run(
    engine: AsyncEngine,
    registry: RetentionRegistry,
    world: HireWorld,
    store: ObjectStore | None,
) -> tuple[dict[str, int], int]:
    _, deleted, failed = await run_org(
        create_sessionmaker(engine), registry, world.org_id, run_id=uuid.uuid4(), store=store
    )
    return deleted, failed


async def _erased(owner: AsyncEngine, world: HireWorld, candidate: uuid.UUID) -> bool:
    ((erased_at,),) = await owner_rows(
        owner, world.org_id, "SELECT erased_at FROM hire_candidates WHERE id = :c", {"c": candidate}
    )
    return erased_at is not None


async def _apply(api: HireApi, world: HireWorld, job: dict[str, Any], label: str) -> str:
    ip = f"192.0.2.{10 + sum(label.encode()) % 200}"
    return await api.apply(world, job["slug"], cv_pdf(label), email=f"{label}@example.test", ip=ip)


# ------------------------------------------------------------------ the whole path


async def test_a_rejected_candidate_is_listed_for_a_week_then_only_the_trail_stays(
    screen_factory: ScreenFactory,
    screened: HireWorld,
    owner_engine: AsyncEngine,
) -> None:
    world = screened
    api = await screen_factory()
    store = api.services.store
    assert isinstance(store, InMemoryObjectStore)
    registry = api.app.state.retention_registry
    assert RULE_KEY in registry
    job = await api.open_job(world)
    app_id = await _shortlisted(api, world, job, CANDIDATE)
    other_id = await _shortlisted(api, world, job, OTHER)
    contact = contact_for(spec(CANDIDATE))
    name, email, phone = contact["full_name"], contact["email"], contact["phone"]
    cid, _ = await _gather(api, world, owner_engine, app_id, name, email)
    sentence = await _sentence(store, [name, email, phone])
    needles = {"name": name, "email": email, "phone": phone, "cv sentence": sentence}
    references = {
        str(r.id): r.reference
        for r in await owner_rows(
            owner_engine, world.org_id, "SELECT id::text AS id, reference FROM hire_applications"
        )
    }
    # Default 12 months: rejected 13 months ago is due, 11 months ago is not.
    await _close(owner_engine, world, references[app_id], 13)
    kept = await _close(owner_engine, world, references[other_id], 11)

    deleted, failed = await _run(api.engine, registry, world, store)
    assert (deleted.get(RULE_KEY), failed) == (0, 0)  # listed, not deleted
    schedule = await api.call("GET", f"{BASE}/schedule", world, "admin")
    assert schedule.status_code == 200, schedule.text
    (item,) = schedule.json()
    assert (item["subject_type"], item["subject_id"], item["status"]) == (
        "hire.candidate",
        str(cid),
        "scheduled",
    )
    assert item["label"] == references[app_id]  # a reference, never a name
    report = (await api.call("GET", f"{BASE}/report", world, "auditor", otp=False)).json()
    (row,) = [r for r in report["rules"] if r["rule"] == RULE_KEY]
    assert (row["would_delete"], row["enabled"], row["deleted"]) == (1, True, 0)
    assert row["scheduled_for"] == item["delete_after"]
    assert report["preview_days"] == 7
    assert not await _erased(owner_engine, world, cid)
    before = await _haystack(owner_engine, world.org_id, store)
    for label, needle in needles.items():
        assert _found(before, needle), f"the {label} is not stored anywhere to begin with"

    await _ended_preview(owner_engine, world)
    deleted, failed = await _run(api.engine, registry, world, store)
    assert (deleted[RULE_KEY], failed) == (1, 0)
    assert await _erased(owner_engine, world, cid)
    assert not await _erased(owner_engine, world, kept)
    after = await _haystack(owner_engine, world.org_id, store)
    for label, needle in needles.items():
        assert not _found(after, needle), f"the {label} is still in {_found(after, needle)}"

    # The trail: a decision record by the system actor, the audit events, the list row.
    (decision,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id, actor_type, actor_id, kind, final_action, workflow, inputs, output "
        "FROM decision_records WHERE entity_type = 'hire.candidate' AND entity_id = :c",
        {"c": str(cid)},
    )
    assert (decision.actor_type, decision.actor_id) == ("system", "pairly.retention")
    assert (decision.kind, decision.final_action, decision.workflow) == (
        "final_action",
        "retention.deleted",
        "hire",
    )
    assert decision.inputs["rule"] == RULE_KEY
    assert decision.inputs["period"] == 12
    assert decision.output["erased"] is True
    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :c ORDER BY seq",
            {"c": str(cid)},
        )
    ]
    assert "core.retention.subject_erased" in actions
    assert "core.retention.subject_deleted" in actions
    done = (await api.call("GET", f"{BASE}/schedule?include_done=true", world, "admin")).json()
    assert [(i["status"], i["decision_id"]) for i in done] == [("deleted", decision.id)]
    # Nothing is listed again: an erased candidate is never due twice.
    deleted, _ = await _run(api.engine, registry, world, store)
    assert deleted[RULE_KEY] == 0
    assert (await api.call("GET", f"{BASE}/schedule", world, "admin")).json() == []


# ------------------------------------------------------------------ holds, cap, switch


async def test_a_legal_hold_blocks_deletion_until_released(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    job = await open_job(api, world)
    held = await _close(owner_engine, world, await _apply(api, world, job, "held"), 14)
    withdrawn = await _close(
        owner_engine, world, await _apply(api, world, job, "withdrawn"), 13, status="withdrawn"
    )
    await _run(api.engine, registry, world, api.store)
    placed = await api.call(
        "POST",
        f"{BASE}/holds",
        world,
        "admin",
        json={
            "subject_type": "hire.candidate",
            "subject_id": str(held),
            "reason": "Employment tribunal claim, keep until it closes.",
        },
    )
    assert placed.status_code == 201, placed.text
    hold = placed.json()
    assert hold["reason"].startswith("Employment tribunal")
    # Without a second factor, or as an auditor: no.
    for who, otp in (("admin", False), ("auditor", True)):
        refused = await api.call(
            "POST",
            f"{BASE}/holds",
            world,
            who,
            otp=otp,
            json={"subject_type": "hire.candidate", "subject_id": str(withdrawn), "reason": "x"},
        )
        assert refused.status_code == 403, (who, refused.text)
    again = await api.call(
        "POST",
        f"{BASE}/holds",
        world,
        "admin",
        json={"subject_type": "hire.candidate", "subject_id": str(held), "reason": "twice"},
    )
    assert again.status_code == 409
    bad = await api.call(
        "POST",
        f"{BASE}/holds",
        world,
        "admin",
        json={"subject_type": "core.users", "subject_id": str(held), "reason": "no"},
    )
    assert bad.status_code == 400
    if "pay.vendor" in registry.hold_subjects:
        vendor = await api.call(
            "POST",
            f"{BASE}/holds",
            world,
            "admin",
            json={"subject_type": "pay.vendor", "subject_id": str(uuid.uuid4()), "reason": "Audit"},
        )
        assert vendor.status_code == 201, vendor.text
    statuses = {
        i["subject_id"]: i["status"]
        for i in (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    }
    assert statuses == {str(held): "held", str(withdrawn): "scheduled"}
    auditor_view = (await api.call("GET", f"{BASE}/holds", world, "auditor", otp=False)).json()
    assert {h["subject_id"] for h in auditor_view} >= {str(held)}
    assert all(h["reason"] is None for h in auditor_view)  # the reason is for admins

    await _ended_preview(owner_engine, world)
    deleted, failed = await _run(api.engine, registry, world, api.store)
    assert (deleted[RULE_KEY], failed) == (1, 0)
    assert await _erased(owner_engine, world, withdrawn)  # withdrawn counts too
    assert not await _erased(owner_engine, world, held)

    released = await api.call("POST", f"{BASE}/holds/{hold['id']}/release", world, "admin")
    assert released.status_code == 200, released.text
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert (item["subject_id"], item["status"]) == (str(held), "scheduled")
    # A fresh preview after the release: the next run deletes nothing yet.
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 0
    assert not await _erased(owner_engine, world, held)
    audit = {
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_type = 'retention_hold'",
        )
    }
    assert audit == {"core.retention.hold_placed", "core.retention.hold_released"}


async def test_the_daily_cap_and_a_rule_switched_off(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    job = await open_job(api, world)
    candidates = [
        await _close(owner_engine, world, await _apply(api, world, job, f"cap-{i}"), 13)
        for i in range(3)
    ]
    settings = await api.call("GET", f"{BASE}/settings", world, "admin")
    assert settings.status_code == 200, settings.text
    body = settings.json()
    assert body["daily_cap"] == 200
    (rule,) = [r for r in body["rules"] if r["key"] == RULE_KEY]
    assert rule["enabled"] is True
    assert rule["period"] == {
        "unit": "months",
        "value": 12,
        "default": 12,
        "minimum": 1,
        "maximum": 120,
    }
    capped = await api.call("PUT", f"{BASE}/settings", world, "admin", json={"daily_cap": 2})
    assert capped.status_code == 200, capped.text
    assert capped.json()["daily_cap"] == 2
    assert (
        await api.call("PUT", f"{BASE}/settings", world, "admin", otp=False, json={"daily_cap": 3})
    ).status_code == 403

    await _run(api.engine, registry, world, api.store)
    await _ended_preview(owner_engine, world)
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 2
    # The cap is per day: a second run the same day deletes no more.
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 0
    assert [await _erased(owner_engine, world, c) for c in candidates].count(True) == 2

    # Switched off: the list empties at once and nothing more is deleted.
    off = await api.call(
        "PUT",
        f"{BASE}/settings",
        world,
        "admin",
        json={"rules": [{"rule": RULE_KEY, "enabled": False}]},
    )
    assert off.status_code == 200, off.text
    assert (await api.call("GET", f"{BASE}/schedule", world, "admin")).json() == []
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE retention_schedule SET erased_at = erased_at - interval '2 days'",
    )
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert RULE_KEY not in deleted
    assert [await _erased(owner_engine, world, c) for c in candidates].count(True) == 2
    # Changing the period goes through the Hire setting (months), audited.
    months = await api.call(
        "PUT",
        f"{BASE}/settings",
        world,
        "admin",
        json={"rules": [{"rule": RULE_KEY, "period": 24}]},
    )
    assert months.status_code == 200, months.text
    hire_settings = await api.call("GET", "/v1/hire/settings", world, "admin")
    assert hire_settings.json()["rejected_retention_months"] == 24
    wrong = await api.call(
        "PUT",
        f"{BASE}/settings",
        world,
        "admin",
        json={"rules": [{"rule": RULE_KEY, "days": 30}]},
    )
    assert wrong.status_code == 400
    changed = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'core.retention.settings_changed'",
    )
    assert len(changed) == 3


# ------------------------------------------------------------------ fail closed


async def test_a_failing_erasure_changes_nothing_and_a_failing_store_is_retried(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry: RetentionRegistry = api.app.state.retention_registry
    job = await open_job(api, world)
    reference = await _apply(api, world, job, "fails")
    candidate = await _close(owner_engine, world, reference, 13)
    rule = registry.get(RULE_KEY)
    assert rule is not None
    assert rule.subjects is not None

    async def half_then_fail(session: Any, subject_id: uuid.UUID, actor: str) -> Mapping[str, int]:
        await erase_due_candidate(session, subject_id, actor)
        raise RuntimeError("the disk caught fire")

    def with_subjects(subjects: SubjectRule) -> RetentionRegistry:
        changed: RetentionRule = dataclasses.replace(rule, subjects=subjects)
        return RetentionRegistry([r if r.key != RULE_KEY else changed for r in registry.rules])

    failing = with_subjects(dataclasses.replace(rule.subjects, erase=half_then_fail))
    await _run(api.engine, failing, world, api.store)
    await _ended_preview(owner_engine, world)
    deleted, failed = await _run(api.engine, failing, world, api.store)
    assert (deleted[RULE_KEY], failed) == (0, 1)
    assert not await _erased(owner_engine, world, candidate)  # rolled back as a whole
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert (item["status"], item["attempts"], item["error"]) == (
        "scheduled",
        1,
        "erase_failed:RuntimeError",
    )
    assert not await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT 1 FROM decision_records WHERE final_action = 'retention.deleted'",
    )

    class BrokenStore(InMemoryObjectStore):
        async def delete(self, key: str) -> None:
            raise StorageError("store down")

    broken = BrokenStore(objects=api.store.objects)
    deleted, failed = await _run(api.engine, registry, world, broken)
    assert (deleted[RULE_KEY], failed) == (1, 0)
    assert await _erased(owner_engine, world, candidate)
    (item,) = (await api.call("GET", f"{BASE}/schedule", world, "admin")).json()
    assert (item["status"], item["error"]) == ("erasing", "object_store_delete_failed")
    deleted, _ = await _run(api.engine, registry, world, api.store)
    done = (await api.call("GET", f"{BASE}/schedule?include_done=true", world, "admin")).json()
    assert [(i["status"], i["error"]) for i in done] == [("deleted", None)]
    (doc,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT d.objects_erased_at FROM documents d JOIN hire_applications a "
        "ON a.cv_document_id = d.id WHERE a.reference = :r",
        {"r": reference},
    )
    assert doc.objects_erased_at is not None


# ------------------------------------------------------------------ rows and tenants


async def test_technical_rows_go_after_the_preview_and_tenants_stay_apart(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    registry = api.app.state.retention_registry
    for w in (world, other):
        await owner_rows(
            owner_engine,
            w.org_id,
            "INSERT INTO inbound_events (org_id, provider, event_id, received_at) VALUES "
            "(:o, 'stripe', 'old', now() - interval '100 days'), "
            "(:o, 'stripe', 'recent', now() - interval '92 days'), "
            "(:o, 'stripe', 'new', now() - interval '1 day')",
            {"o": w.org_id},
        )
    job = await open_job(api, world)
    mine = await _close(owner_engine, world, await _apply(api, world, job, "tenant"), 13)

    deleted, _ = await _run(api.engine, registry, world, api.store)
    # 90 days plus the seven-day preview: only the event of 100 days goes now.
    assert deleted["inbound_events.seen"] == 1
    left = {
        r.event_id
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT event_id FROM inbound_events WHERE org_id = :o",
            {"o": world.org_id},
        )
    }
    assert left == {"recent", "new"}
    (decision,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_id, output FROM decision_records WHERE entity_type = 'retention_rule' "
        "AND entity_id = 'inbound_events.seen'",
    )
    assert (decision.actor_id, decision.output) == ("pairly.retention", {"deleted_rows": 1})

    # The other organisation: its own list (empty), its own rows untouched by our run.
    theirs = {
        r.event_id
        for r in await owner_rows(
            owner_engine,
            other.org_id,
            "SELECT event_id FROM inbound_events WHERE org_id = :o",
            {"o": other.org_id},
        )
    }
    assert theirs == {"old", "recent", "new"}
    assert (await api.call("GET", f"{BASE}/schedule", other, "admin")).json() == []
    hold = await api.call(
        "POST",
        f"{BASE}/holds",
        other,
        "admin",
        json={"subject_type": "hire.candidate", "subject_id": str(mine), "reason": "Not ours"},
    )
    assert hold.status_code == 201  # it names an id; it holds nothing of this organisation
    await _run(api.engine, registry, other, api.store)
    await _ended_preview(owner_engine, world)
    deleted, _ = await _run(api.engine, registry, world, api.store)
    assert deleted[RULE_KEY] == 1
    assert await _erased(owner_engine, world, mine)
    assert [
        h["subject_id"] for h in (await api.call("GET", f"{BASE}/holds", world, "admin")).json()
    ] == []
    other_settings = await api.call(
        "PUT", f"{BASE}/settings", other, "admin", json={"daily_cap": 5}
    )
    assert other_settings.status_code == 200
    mine_settings = (await api.call("GET", f"{BASE}/settings", world, "admin")).json()
    assert mine_settings["daily_cap"] == 200
    # Nobody but admins and auditors.
    assert (await api.call("GET", f"{BASE}/schedule", world, "recruiter")).status_code == 403

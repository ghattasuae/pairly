# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire pipeline (WP2.5) and messaging (WP2.7) on Postgres, through the API.

* Stage moves: audited, people only with a second factor, history kept; undo within a
  minute by the same person; customised stages.
* A rejection is never sent without an approval: the move drafts it, the recruiter
  submits it, a different person approves it with a second factor, and only then the
  email is queued (through the outbox) with the approval's payload hash.
* The approval binds the exact text: a template changed after the approval blocks it.
* Bulk: a summary before anything happens; one undo for the batch.
* Notes with @mentions notify the people mentioned in the app.
* Blind mode (screening's, wired through ``pipeline.hooks``) hides identity until a
  stage reveals it.
* Talent rediscovery: permission, consent and retention filters.
* Referrals: tracking, and the referrer sees the stage only.
* AI drafts from recordings fill only the personal paragraph; a draft that adds facts
  is refused and nothing changes.
* The anti-ghosting SLA drafts a status update and alerts the recruiter once.
* Candidate preferences: status updates can be turned off; outcomes are still sent.
* Tenant isolation of every new table.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.outbox import OutboxTransport
from pairly_core.rbac import build_registry
from pairly_core.retrieval.embeddings import HashingEmbedder
from pairly_hire import links as hire_links
from pairly_hire.demo import BACKEND
from pairly_hire.messaging import library
from pairly_hire.messaging.recordings import bundle_text, recordings_text
from pairly_hire.messaging.sla import draft_overdue
from pairly_hire.pipeline.rediscovery import sync_org
from pairly_hire.recordings import entry_for
from pairly_hire.screening import reviewer as screening_reviewer

from .hire_support import (
    PLUGINS,
    HireApi,
    HireWorld,
    cv_pdf,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .notify_support import LINK_SECRET, notify_settings

__all__ = ["hire_api"]  # fixture

NEW_TABLES = (
    "hire_pipeline_configs",
    "hire_pipeline_entries",
    "hire_stage_moves",
    "hire_notes",
    "hire_referral_links",
    "hire_referrals",
    "hire_messages",
    "hire_candidate_prefs",
    "hire_messaging_settings",
)
REGISTRY = build_registry(PLUGINS)


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture(autouse=True)
def _reset_hooks() -> Any:
    yield
    # Back to what the plugin installs: screening's blind mode and results.
    screening_reviewer.register()


_IP = iter(range(10, 250))


async def apply_many(
    api: HireApi, world: HireWorld, job: dict[str, Any], names: list[str], **kw: Any
) -> dict[str, str]:
    """Apply once per name; returns ``{name: application id}``."""
    refs: dict[str, str] = {}
    for name in names:
        slug = name.lower().replace(" ", ".")
        refs[name] = await api.apply(
            world,
            job["slug"],
            cv_pdf(f"{name}-{uuid.uuid4().hex}"),
            email=f"{slug}.{uuid.uuid4().hex[:6]}@example.test",
            full_name=name,
            ip=f"198.51.100.{next(_IP)}",
            **kw,
        )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    by_ref = {a["reference"]: a["id"] for a in listed.json()["items"]}
    return {name: by_ref[ref] for name, ref in refs.items()}


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


async def move(
    api: HireApi,
    world: HireWorld,
    app_id: str,
    to: str,
    *,
    who: str = "recruiter",
    otp: bool = True,
) -> Any:
    return await api.client.post(
        f"/v1/hire/pipeline/applications/{app_id}/move",
        json={"to_stage": to},
        headers=api.headers(world, who, otp=otp, extra=_key()),
    )


async def board(api: HireApi, world: HireWorld, job_id: str, who: str = "recruiter") -> Any:
    response = await api.call("GET", f"/v1/hire/jobs/{job_id}/pipeline", world, who)
    assert response.status_code == 200, response.text
    return response.json()


def stage_of(data: dict[str, Any], app_id: str) -> str:
    return str(next(c for c in data["cards"] if c["application_id"] == app_id)["stage_key"])


async def deliver(api: HireApi, world: HireWorld, outbox: Path) -> list[dict[str, Any]]:
    await deliver_org(
        create_sessionmaker(api.engine),
        world.org_id,
        Transports(
            settings=notify_settings(), email=OutboxTransport(outbox), slack=FakeSlackTransport()
        ),
    )
    return [json.loads(p.read_text()) for p in sorted(outbox.glob("*.json"))]


# ------------------------------------------------------------------ moves and undo


async def test_stage_moves_are_audited_and_need_a_person_with_a_second_factor(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await apply_many(api, world, job, ["Layla Haddad", "Omar Saleh"])
    layla = ids["Layla Haddad"]
    data = await board(api, world, job["id"])
    assert [s["key"] for s in data["stages"]][:3] == ["applied", "screening", "shortlist"]
    assert {stage_of(data, i) for i in ids.values()} == {"applied"}
    assert data["sla_days"] == 7

    advanced = await api.client.post(
        f"/v1/hire/pipeline/applications/{layla}/advance",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert advanced.status_code == 200, advanced.text
    assert (advanced.json()["from_stage"], advanced.json()["to_stage"]) == ("applied", "screening")
    assert advanced.json()["undo_seconds"] >= 10
    assert stage_of(await board(api, world, job["id"]), layla) == "screening"

    # Without a second factor, as an interviewer, as an agent role: refused.
    assert (await move(api, world, layla, "shortlist", otp=False)).status_code == 403
    assert (await move(api, world, layla, "shortlist", who="interviewer")).status_code == 403
    assert (await move(api, world, layla, "no_such_stage")).status_code == 400
    assert (await move(api, world, layla, "screening")).status_code == 409

    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT from_stage, to_stage, actor_type FROM hire_stage_moves WHERE application_id = :a",
        {"a": layla},
    )
    assert [(r.from_stage, r.to_stage, r.actor_type) for r in rows] == [
        ("applied", "screening", "user")
    ]
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action, payload FROM audit_events "
        "WHERE entity_id = :a AND action LIKE 'hire.candidate.%'",
        {"a": layla},
    )
    assert [(e.action, e.payload["from"], e.payload["to"]) for e in events] == [
        ("hire.candidate.moved", "applied", "screening")
    ]


async def test_undo_reverts_the_same_persons_recent_move(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Sara Nasser"]))["Sara Nasser"]
    moved = (await move(api, world, app_id, "shortlist")).json()

    # Someone else cannot undo it.
    other = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "manager",
        json={"move_ids": [moved["move_id"]]},
    )
    assert other.status_code == 403
    undone = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [moved["move_id"]]},
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["undone"][0]["to_stage"] == "applied"
    assert stage_of(await board(api, world, job["id"]), app_id) == "applied"
    again = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [moved["move_id"]]},
    )
    assert again.status_code == 409

    # Too late: past the undo window.
    late = (await move(api, world, app_id, "screening")).json()
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_stage_moves SET created_at = now() - interval '5 minutes' WHERE id = :m",
        {"m": late["move_id"]},
    )
    expired = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [late["move_id"]]},
    )
    assert expired.status_code == 409
    actions = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action FROM audit_events WHERE entity_id = :a ORDER BY seq",
        {"a": app_id},
    )
    assert [a.action for a in actions if a.action.startswith("hire.candidate.")] == [
        "hire.candidate.moved",
        "hire.candidate.move_undone",
        "hire.candidate.moved",
    ]


async def test_custom_stages(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Noor Ali"]))["Noor Ali"]
    stages: list[dict[str, Any]] = [
        {"key": "applied", "label": "New"},
        {"key": "phone_screen", "label": "Phone screen", "label_ar": "مكالمة"},
        {"key": "case_study", "label": "Case study", "reveal_identity": True},
        {"key": "hired", "label": "Hired", "kind": "hired"},
        {"key": "rejected", "label": "Not selected", "kind": "rejected"},
        {"key": "withdrawn", "label": "Withdrawn", "kind": "withdrawn"},
    ]
    bad = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/stages", world, "recruiter", json=stages[1:]
    )
    assert bad.status_code == 400
    denied = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/stages", world, "manager", json=stages
    )
    assert denied.status_code == 403
    saved = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/stages", world, "recruiter", json=stages
    )
    assert saved.status_code == 200, saved.text
    advanced = await api.client.post(
        f"/v1/hire/pipeline/applications/{app_id}/advance",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert advanced.json()["to_stage"] == "phone_screen"
    # A stage that holds a candidate cannot be removed.
    without = [s for s in stages if s["key"] != "phone_screen"]
    blocked = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/stages", world, "recruiter", json=without
    )
    assert blocked.status_code == 409


# ------------------------------------------------------------------ rejection: approval first


async def test_rejection_is_never_sent_without_an_approval(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Layla Haddad"]))["Layla Haddad"]
    outbox = tmp_path / "outbox"
    acknowledgements = await deliver(api, world, outbox)
    assert len(acknowledgements) == 1  # only the acknowledgement so far

    moved = await move(api, world, app_id, "rejected")
    assert moved.status_code == 200, moved.text
    draft_id = moved.json()["draft_id"]
    assert draft_id
    # Moving sent nothing, and the application is rejected with a retention date.
    assert len(await deliver(api, world, outbox)) == 1
    (app,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status, retention_until FROM hire_applications WHERE id = :a",
        {"a": app_id},
    )
    assert app.status == "rejected"
    assert app.retention_until is not None

    message = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert message["template_key"] == library.REJECTION_SCREENING
    assert message["sensitive"] is True
    assert message["status"] == "draft"
    assert message["preview"]["ok"]
    # Blind until the shortlist: the preview shows the label; the email carries the name.
    assert message["preview"]["masked"]
    assert "Layla Haddad" not in message["preview"]["text"]
    assert "Dear Candidate " in message["preview"]["text"]

    # A sensitive message cannot be sent directly.
    direct = await api.client.post(
        f"/v1/hire/messages/{draft_id}/send", headers=api.headers(world, "recruiter", extra=_key())
    )
    assert direct.status_code == 409

    submitted = await api.client.post(
        f"/v1/hire/messages/{draft_id}/submit",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert submitted.status_code == 200, submitted.text
    after = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert after["status"] == "awaiting_approval", after
    approval_id = after["approval_id"]
    assert len(await deliver(api, world, outbox)) == 1  # still nothing sent

    detail = (await api.call("GET", f"/v1/approvals/{approval_id}", world, "manager")).json()
    assert detail["approval_type"] == "hire.message"
    # The approval binds the exact email, which carries the name; the reviewer's
    # preview shows the blind label in its place.
    label = after["preview"]["text"].split("\n", 1)[0].removeprefix("Dear ").rstrip(",")
    assert label.startswith("Candidate ")
    assert detail["payload"]["text"] == after["preview"]["text"].replace(label, "Layla Haddad")

    # The submitter cannot approve their own message; nobody can without a second factor.
    own = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert own.status_code == 403
    no_otp = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "manager", otp=False, extra=_key()),
    )
    assert no_otp.status_code == 403
    approved = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert approved.status_code == 200, approved.text

    sent = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert sent["status"] == "queued", sent
    mails = await deliver(api, world, outbox)
    assert len(mails) == 2
    rejection = next(
        m for m in mails if "Layla Haddad" in m["text"] and "not to move forward" in m["text"]
    )
    assert rejection["text"] == detail["payload"]["text"]
    (note,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT n.content_hash, a.payload_hash, n.approval_id FROM notifications n "
        "JOIN approvals a ON a.id = n.approval_id WHERE n.approval_id = :a",
        {"a": approval_id},
    )
    assert note.content_hash == note.payload_hash
    actions = {
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :m",
            {"m": draft_id},
        )
    }
    assert {
        "hire.message.drafted",
        "hire.message.submitted",
        "hire.message.awaiting_approval",
        "hire.message.sent",
    } <= actions


async def test_a_template_changed_after_submission_blocks_the_send(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Omar Saleh"]))["Omar Saleh"]
    draft_id = (await move(api, world, app_id, "rejected")).json()["draft_id"]
    await api.client.post(
        f"/v1/hire/messages/{draft_id}/submit",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    approval_id = (
        await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")
    ).json()["approval_id"]
    # An admin saves a new version of the rejection template before the approval.
    template = library.definitions(library.REJECTION_SCREENING)[0]
    saved = await api.call(
        "POST",
        "/v1/notification-templates",
        world,
        "admin",
        json={
            "key": template.key,
            "channel": "email",
            "locale": "en",
            "subject": template.subject,
            "body": template.body + "\n\nP.S. A line nobody approved.",
            "variables": template.variables,
            "requires_approval": True,
        },
    )
    assert saved.status_code in {200, 201}, saved.text
    approved = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert approved.status_code == 200, approved.text
    message = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert message["status"] == "failed"
    assert message["status_reason"].startswith("refused:sensitive_payload_hash_mismatch")
    sent = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM notifications WHERE approval_id = :a",
        {"a": approval_id},
    )
    assert sent == []


async def test_declined_rejection_is_not_sent(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Huda Kareem"]))["Huda Kareem"]
    draft_id = (await move(api, world, app_id, "rejected")).json()["draft_id"]
    await api.client.post(
        f"/v1/hire/messages/{draft_id}/submit",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    approval_id = (
        await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")
    ).json()["approval_id"]
    declined = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "reject", "reason": "Wrong template for this stage."},
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert declined.status_code == 200, declined.text
    message = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert message["status"] == "declined"
    assert (
        await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT id FROM notifications WHERE approval_id = :a",
            {"a": approval_id},
        )
        == []
    )


# ------------------------------------------------------------------ bulk


async def test_bulk_summary_before_anything_and_one_undo(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await apply_many(api, world, job, ["A One", "B Two", "C Three"])
    chosen = list(ids.values())
    await move(api, world, chosen[2], "rejected")
    summary = await api.call(
        "POST",
        f"/v1/hire/jobs/{job['id']}/pipeline/bulk/preview",
        world,
        "recruiter",
        json={"application_ids": chosen, "to_stage": "rejected"},
    )
    assert summary.status_code == 200, summary.text
    data = summary.json()
    assert (data["total"], data["will_move"], data["drafts_to_create"]) == (3, 2, 2)
    assert data["already_there"] == [chosen[2]]
    assert data["messages_sent_now"] == 0
    assert data["needs_approval"] is True
    # The preview changed nothing.
    assert stage_of(await board(api, world, job["id"]), chosen[0]) == "applied"

    done = await api.client.post(
        f"/v1/hire/jobs/{job['id']}/pipeline/bulk/move",
        json={"application_ids": chosen, "to_stage": "rejected"},
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert done.status_code == 200, done.text
    batch = done.json()
    assert len(batch["moves"]) == 2
    assert all(m["draft_id"] for m in batch["moves"])
    sent = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM notifications "
        "WHERE template_key LIKE 'hire.candidate.rejection%'",
    )
    assert sent[0].n == 0
    undone = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [m["move_id"] for m in batch["moves"]]},
    )
    assert undone.status_code == 200, undone.text
    assert len(undone.json()["cancelled_drafts"]) == 2
    data = await board(api, world, job["id"])
    assert [stage_of(data, i) for i in chosen] == ["applied", "applied", "rejected"]

    # Bulk status updates: a summary, and sensitive templates are never sent in bulk.
    preview = await api.call(
        "POST",
        f"/v1/hire/jobs/{job['id']}/messages/bulk/preview",
        world,
        "recruiter",
        json={"application_ids": chosen[:2], "template_key": library.STATUS_UPDATE},
    )
    assert preview.json()["will_send"] == 2
    assert not preview.json()["sensitive"]
    refused = await api.client.post(
        f"/v1/hire/jobs/{job['id']}/messages/bulk/send",
        json={"application_ids": chosen[:2], "template_key": library.REJECTION},
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert refused.status_code == 409
    bulk = await api.client.post(
        f"/v1/hire/jobs/{job['id']}/messages/bulk/send",
        json={"application_ids": chosen[:2], "template_key": library.STATUS_UPDATE},
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["sent"] == 2


# ------------------------------------------------------------------ notes, tags, mentions


async def test_notes_mention_people_in_the_app(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Rania Fares"]))["Rania Fares"]
    team = (await api.call("GET", "/v1/hire/pipeline/team", world, "recruiter")).json()
    manager_id = str(world.people["manager"][0])
    employee_id = str(world.people["employee"][0])
    assert manager_id in {m["id"] for m in team}
    assert employee_id not in {m["id"] for m in team}

    note = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/notes",
        world,
        "recruiter",
        json={
            "body": "Strong Python. @manager can you look at the case study?",
            "mentions": [manager_id],
        },
    )
    assert note.status_code == 201, note.text
    assert note.json()["mentioned"] == [manager_id]
    refused = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/notes",
        world,
        "recruiter",
        json={"body": "@employee", "mentions": [employee_id]},
    )
    assert refused.status_code == 400
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT channel, template_key, recipient_user_id FROM notifications "
        "WHERE template_key = 'hire.note.mention'",
    )
    assert [(r.channel, str(r.recipient_user_id)) for r in rows] == [("in_app", manager_id)]
    # The audit trail never holds the note's text.
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.note.added'",
    )
    assert "Python" not in json.dumps([e.payload for e in events])

    tags = await api.call(
        "PUT",
        f"/v1/hire/pipeline/applications/{app_id}/tags",
        world,
        "interviewer",
        json={"tags": ["python", "Python", "relocating"]},
    )
    assert tags.status_code == 200, tags.text
    assert tags.json()["tags"] == ["python", "relocating"]
    profile = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "manager")
    ).json()
    kinds = [e["kind"] for e in profile["timeline"]]
    assert "note" in kinds
    assert "application" in kinds
    assert profile["tags"] == ["python", "relocating"]
    note_event = next(e for e in profile["timeline"] if e["kind"] == "note")
    assert note_event["detail"]["mentions"]


# ------------------------------------------------------------------ blind mode


async def test_blind_mode_hides_identity_until_a_stage_reveals_it(
    hire_api: HireApi, world: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Yusuf Amin"]))["Yusuf Amin"]
    # Blind mode is on by default (screening's setting) and nothing screened it yet.
    data = await board(api, world, job["id"])
    card = next(c for c in data["cards"] if c["application_id"] == app_id)
    assert data["blind_mode"]
    assert card["identity_hidden"]
    assert card["label"].startswith("Candidate ")
    assert "Yusuf" not in json.dumps(data)
    profile = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    ).json()
    assert profile["identity_hidden"]
    assert profile["contact"] is None
    assert profile["files"] == []
    assert "Yusuf" not in json.dumps(profile)

    # A draft's preview does not show the name either (the email will carry it).
    draft = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/messages",
        world,
        "recruiter",
        json={"template_key": library.STATUS_UPDATE},
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["preview"]["masked"]
    assert "Yusuf" not in draft.json()["preview"]["text"]

    await move(api, world, app_id, "shortlist")
    revealed = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    ).json()
    assert not revealed["identity_hidden"]
    assert revealed["contact"]["full_name"] == "Yusuf Amin"
    # Once revealed, a move back does not hide it again.
    await move(api, world, app_id, "screening")
    back = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    ).json()
    assert not back["identity_hidden"]


# ------------------------------------------------------------------ rediscovery


async def _prefs(api: HireApi, world: HireWorld, candidate_id: Any, **changes: bool) -> Any:
    token = hire_links.prefs_token(LINK_SECRET, world.org_id, uuid.UUID(str(candidate_id)))
    return await api.client.post(
        "/v1/hire/public/preferences", json={"token": token, **changes}, headers=api.public()
    )


async def test_rediscovery_respects_permission_consent_and_retention(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    api.app.state.policy_embedder = HashingEmbedder()
    job = await open_job(api, world)
    ids = await apply_many(api, world, job, ["Kubernetes Fan", "Quiet Person", "Expired Person"])
    candidates = {
        name: (
            await owner_rows(
                owner_engine,
                world.org_id,
                "SELECT candidate_id FROM hire_applications WHERE id = :a",
                {"a": app},
            )
        )[0].candidate_id
        for name, app in ids.items()
    }
    for app in ids.values():
        await api.call(
            "PUT",
            f"/v1/hire/pipeline/applications/{app}/tags",
            world,
            "recruiter",
            json={"tags": ["kubernetes", "platform"]},
        )
    for name in ("Kubernetes Fan", "Expired Person"):
        changed = await _prefs(api, world, candidates[name], talent_pool=True)
        assert changed.status_code == 200, changed.text
        assert changed.json()["talent_pool"] is True
    # The expired one was rejected long ago: past its retention date.
    await move(api, world, ids["Expired Person"], "rejected")
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_applications SET retention_until = now() - interval '1 day' WHERE id = :a",
        {"a": ids["Expired Person"]},
    )

    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        counts = await sync_org(session, embedder=HashingEmbedder(), store=None)
    assert counts["indexed"] == 1

    found = await api.call(
        "POST", "/v1/hire/rediscovery/search", world, "recruiter", json={"query": "kubernetes"}
    )
    assert found.status_code == 200, found.text
    hits = found.json()
    assert [h["candidate_id"] for h in hits] == [str(candidates["Kubernetes Fan"])]
    assert hits[0]["applications"][0]["job_title"] == job["title"]
    assert "@" not in hits[0]["snippet"]
    denied = await api.call(
        "POST", "/v1/hire/rediscovery/search", world, "employee", json={"query": "kubernetes"}
    )
    assert denied.status_code == 403

    # Consent withdrawn: removed from the index at once.
    await _prefs(api, world, candidates["Kubernetes Fan"], talent_pool=False)
    again = await api.call(
        "POST", "/v1/hire/rediscovery/search", world, "recruiter", json={"query": "kubernetes"}
    )
    assert again.json() == []
    chunks = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM search_chunks WHERE kind = 'hire_candidate'",
    )
    assert chunks[0].n == 0


# ------------------------------------------------------------------ referrals


async def test_referrals_are_tracked_and_the_referrer_sees_the_stage_only(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    link = await api.call(
        "POST", "/v1/hire/referrals/links", world, "employee", json={"job_id": job["id"]}
    )
    assert link.status_code == 200, link.text
    url = link.json()["url"]
    assert f"/en/careers/{world.slug}/{job['slug']}?ref=rf1." in url
    token = url.split("?ref=", 1)[1]
    same = await api.call(
        "POST", "/v1/hire/referrals/links", world, "employee", json={"job_id": job["id"]}
    )
    assert same.json()["link_id"] == link.json()["link_id"]

    referred = (await apply_many(api, world, job, ["Lina Referred"], referral=token))[
        "Lina Referred"
    ]
    forged = (await apply_many(api, world, job, ["No Referral"], referral=token[:-3] + "abc"))[
        "No Referral"
    ]
    rows = await owner_rows(
        owner_engine, world.org_id, "SELECT application_id, referrer_id FROM hire_referrals"
    )
    assert [(str(r.application_id), r.referrer_id) for r in rows] == [
        (referred, world.people["employee"][0])
    ]
    data = await board(api, world, job["id"])
    assert next(c for c in data["cards"] if c["application_id"] == referred)["referred"]
    assert not next(c for c in data["cards"] if c["application_id"] == forged)["referred"]

    await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{referred}/notes",
        world,
        "recruiter",
        json={"body": "Private note the referrer must never see.", "mentions": []},
    )
    await move(api, world, referred, "interview")
    mine = await api.call("GET", "/v1/hire/referrals/mine", world, "employee")
    assert mine.status_code == 200, mine.text
    (item,) = mine.json()
    assert set(item) == {
        "referral_id",
        "job_title",
        "candidate",
        "stage",
        "stage_label",
        "referred_at",
    }
    assert (item["stage"], item["stage_label"]) == ("in_progress", "Interview")
    assert "Private note" not in mine.text
    await move(api, world, referred, "rejected")
    (item,) = (await api.call("GET", "/v1/hire/referrals/mine", world, "employee")).json()
    assert item["stage_label"] == "Not selected"
    # The referrer never reads the profile or the board.
    assert (
        await api.call("GET", f"/v1/hire/pipeline/applications/{referred}", world, "employee")
    ).status_code == 403
    assert (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/pipeline", world, "employee")
    ).status_code == 403


# ------------------------------------------------------------------ AI drafts


async def test_ai_drafts_fill_only_the_paragraph_and_bad_drafts_are_refused(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    session = create_sessionmaker(owner_engine)()
    set_tenant(session, world.org_id, None)
    # The demo recordings, plus one bad answer for a job title only this test uses.
    from pairly_hire.messaging.drafting import TASK, MessageDraft, draft_inputs

    bad = entry_for(
        TASK,
        MessageDraft,
        draft_inputs(library.STATUS_UPDATE, "Fact Inventor", "en"),
        MessageDraft(personal_note="Our Abu Dhabi office offers 20 percent more pay."),
    )
    async with session, session.begin():
        await import_recordings(session, world.org_id, parse_bundle(recordings_text()))
        await import_recordings(session, world.org_id, parse_bundle(bundle_text([bad])))
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Dana Musa"]))["Dana Musa"]
    draft = (
        await api.call(
            "POST",
            f"/v1/hire/pipeline/applications/{app_id}/messages",
            world,
            "recruiter",
            json={"template_key": library.STATUS_UPDATE},
        )
    ).json()
    drafted = await api.call(
        "POST", f"/v1/hire/messages/{draft['id']}/ai-draft", world, "recruiter", json={}
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["from_recording"]
    assert body["prompt_version"] == "hire_message_draft/v1"
    message = body["message"]
    assert message["ai_fields"] == ["personal_note"]
    assert set(message["values"]) == {"personal_note"}
    assert BACKEND.draft.title in message["values"]["personal_note"]
    # The record's values come from the record; the paragraph sits in its place.
    assert message["preview"]["text"].startswith("Hello Candidate ")  # blind: masked
    assert message["values"]["personal_note"] in message["preview"]["text"]

    # A person's edit takes the field back from the model.
    edited = await api.call(
        "PATCH",
        f"/v1/hire/messages/{draft['id']}",
        world,
        "recruiter",
        json={"values": {"personal_note": "Thanks for waiting. We will write again soon."}},
    )
    assert edited.json()["ai_fields"] == []

    # A job whose recorded answer invents facts: refused, nothing saved.
    await api.call(
        "PATCH", f"/v1/hire/jobs/{job['id']}", world, "recruiter", json={"title": "Fact Inventor"}
    )
    second = (
        await api.call(
            "POST",
            f"/v1/hire/pipeline/applications/{app_id}/messages",
            world,
            "recruiter",
            json={"template_key": library.STATUS_UPDATE},
        )
    ).json()
    refused = await api.call(
        "POST", f"/v1/hire/messages/{second['id']}/ai-draft", world, "recruiter", json={}
    )
    assert refused.status_code == 400, refused.text
    unchanged = (
        await api.call("GET", f"/v1/hire/messages/{second['id']}", world, "recruiter")
    ).json()
    assert unchanged["values"] == {}
    assert unchanged["ai_fields"] == []
    # No recording at all: the model is unavailable, the person writes it (503).
    await api.call(
        "PATCH", f"/v1/hire/jobs/{job['id']}", world, "recruiter", json={"title": "Unrecorded Role"}
    )
    missing = await api.call(
        "POST", f"/v1/hire/messages/{second['id']}/ai-draft", world, "recruiter", json={}
    )
    assert missing.status_code == 503


# ------------------------------------------------------------------ SLA


async def test_sla_drafts_a_status_update_and_alerts_the_recruiter_once(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await apply_many(api, world, job, ["Old Wait", "New Wait"])
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_applications SET submitted_at = now() - interval '9 days' WHERE id = :a",
        {"a": ids["Old Wait"]},
    )
    waiting = (await api.call("GET", "/v1/hire/pipeline/waiting", world, "recruiter")).json()
    assert waiting["items"][0]["application_id"] == ids["Old Wait"]
    assert waiting["items"][0]["waiting_days"] >= 9
    assert waiting["items"][0]["sla_overdue"]

    config = hire_links.LinkConfig(LINK_SECRET, "https://app.test.pairly")
    for _ in range(2):
        session = create_sessionmaker(api.engine)()
        set_tenant(session, world.org_id, None)
        async with session, session.begin():
            result = await draft_overdue(session, REGISTRY, links=config)
        if _ == 0:
            assert result == {"drafted": 1, "jobs": 1, "alerts": 1}
        else:
            assert result["drafted"] == 0
    drafts = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT application_id, template_key, purpose, status FROM hire_messages",
    )
    assert [(str(d.application_id), d.template_key, d.purpose, d.status) for d in drafts] == [
        (ids["Old Wait"], library.STATUS_UPDATE, "sla_update", "draft")
    ]
    alerts = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT channel, urgency, recipient_user_id FROM notifications "
        "WHERE template_key = 'hire.sla.waiting'",
    )
    recruiter = world.people["recruiter"][0]
    assert {(a.channel, a.urgency, a.recipient_user_id) for a in alerts} == {
        ("in_app", "urgent", recruiter),
        ("email", "urgent", recruiter),
    }
    # Sending the drafted update resets the clock.
    (drafted,) = await owner_rows(owner_engine, world.org_id, "SELECT id FROM hire_messages")
    send = await api.client.post(
        f"/v1/hire/messages/{drafted.id}/send",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert send.status_code == 200, send.text
    waiting = (await api.call("GET", "/v1/hire/pipeline/waiting", world, "recruiter")).json()
    assert all(not w["sla_overdue"] for w in waiting["items"])

    # The setting: admins only.
    assert (
        await api.call(
            "PUT", "/v1/hire/settings/messaging", world, "recruiter", json={"sla_days": 3}
        )
    ).status_code == 403
    changed = await api.call(
        "PUT", "/v1/hire/settings/messaging", world, "admin", json={"sla_days": 3}
    )
    assert changed.json() == {"sla_days": 3}


# ------------------------------------------------------------------ preferences


async def test_candidates_turn_updates_off_but_outcomes_still_go(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Pref Person"]))["Pref Person"]
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT candidate_id FROM hire_applications WHERE id = :a",
        {"a": app_id},
    )
    token = hire_links.prefs_token(LINK_SECRET, world.org_id, row.candidate_id)
    read = await api.client.post(
        "/v1/hire/public/preferences/read", json={"token": token}, headers=api.public()
    )
    assert read.status_code == 200, read.text
    assert read.json() == {
        "organisation": world.name,
        "updates_email": True,
        "talent_pool": False,
        "jobs": [job["title"]],
    }
    forged = await api.client.post(
        "/v1/hire/public/preferences/read",
        json={"token": token[:-4] + "AAAA"},
        headers=api.public(),
    )
    assert forged.status_code == 404
    off = await _prefs(api, world, row.candidate_id, updates_email=False)
    assert off.json()["updates_email"] is False

    update = (
        await api.call(
            "POST",
            f"/v1/hire/pipeline/applications/{app_id}/messages",
            world,
            "recruiter",
            json={"template_key": library.STATUS_UPDATE},
        )
    ).json()
    assert update["candidate_opted_out"]
    sent = await api.client.post(
        f"/v1/hire/messages/{update['id']}/send",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert sent.json()["status"] == "suppressed"
    invite = (
        await api.call(
            "POST",
            f"/v1/hire/pipeline/applications/{app_id}/messages",
            world,
            "recruiter",
            json={
                "template_key": library.INTERVIEW_INVITE,
                "values": {"interview_details": "A 30 minute call on Monday at 11:00."},
            },
        )
    ).json()
    assert invite["preview"]["ok"], invite
    queued = await api.client.post(
        f"/v1/hire/messages/{invite['id']}/send",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert queued.json()["status"] == "queued"
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.candidate.preferences_changed'",
    )
    assert events
    assert events[0].payload["via"] == "signed_link"


# ------------------------------------------------------------------ tenant isolation


async def test_tenant_isolation_of_every_pipeline_and_messaging_table(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await apply_many(api, world, job, ["Iso One", "Iso Two"])
    a, b = ids["Iso One"], ids["Iso Two"]
    await api.call(
        "PUT",
        f"/v1/hire/jobs/{job['id']}/stages",
        world,
        "recruiter",
        json=[
            {"key": "applied", "label": "Applied"},
            {"key": "screening", "label": "Screening review"},
            {"key": "hired", "label": "Hired", "kind": "hired"},
            {"key": "rejected", "label": "Rejected", "kind": "rejected"},
            {"key": "withdrawn", "label": "Withdrawn", "kind": "withdrawn"},
        ],
    )
    await move(api, world, a, "screening")
    await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{a}/notes",
        world,
        "recruiter",
        json={"body": "x", "mentions": []},
    )
    link = await api.call(
        "POST", "/v1/hire/referrals/links", world, "employee", json={"job_id": job["id"]}
    )
    await apply_many(
        api, world, job, ["Iso Three"], referral=link.json()["url"].split("?ref=", 1)[1]
    )
    await move(api, world, b, "rejected")
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT candidate_id FROM hire_applications WHERE id = :a",
        {"a": a},
    )
    await _prefs(api, world, row.candidate_id, talent_pool=True)
    await api.call("PUT", "/v1/hire/settings/messaging", world, "admin", json={"sla_days": 5})

    for table in NEW_TABLES:
        mine = await owner_rows(
            owner_engine,
            world.org_id,
            f"SELECT count(*) AS n FROM {table} WHERE org_id = :o",  # noqa: S608 (fixed names)
            {"o": world.org_id},
        )
        assert mine[0].n > 0, table

    maker = create_sessionmaker(api.engine)
    session = maker()
    set_tenant(session, other.org_id, None)
    async with session, session.begin():
        for table in NEW_TABLES:
            seen = await session.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 (fixed names)
            )
            assert seen.scalar_one() == 0, table
    # Writing another organisation's row is refused by the policy.
    session = maker()
    set_tenant(session, other.org_id, None)
    with pytest.raises(Exception, match="row-level security"):
        async with session, session.begin():
            await session.execute(
                text("INSERT INTO hire_messaging_settings (org_id, sla_days) VALUES (:o, 3)"),
                {"o": world.org_id},
            )
    # And the API answers 404 for another organisation's application.
    assert (
        await api.call("GET", f"/v1/hire/pipeline/applications/{a}", other, "recruiter")
    ).status_code == 404
    assert (await move(api, other, a, "shortlist")).status_code == 404
    # Append-only history: the application role cannot change or delete moves.
    session = maker()
    set_tenant(session, world.org_id, None)
    with pytest.raises(Exception, match="permission denied"):
        async with session, session.begin():
            await session.execute(text("UPDATE hire_stage_moves SET to_stage = 'hired'"))


async def test_a_submission_whose_run_never_started_is_restarted(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    from pairly_core.audit import ActorType
    from pairly_hire.messaging.compose import Author, get_message, submit_message
    from pairly_hire.messaging.graph import restart_stuck
    from pairly_hire.pipeline.records import load_record

    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Stuck Person"]))["Stuck Person"]
    draft_id = (await move(api, world, app_id, "rejected")).json()["draft_id"]
    recruiter = world.people["recruiter"][0]
    maker = create_sessionmaker(api.engine)
    session = maker()
    set_tenant(session, world.org_id, None)
    config = hire_links.LinkConfig(LINK_SECRET, "https://app.test.pairly")
    async with session, session.begin():
        message = await get_message(session, uuid.UUID(draft_id), lock=True)
        record = await load_record(session, message.application_id)
        await submit_message(
            session, message, record, Author(ActorType.USER, str(recruiter)), links=config
        )
    runtime = api.app.state.workflow_runtime
    # Too recent: left alone (the API's own background start may still be running).
    assert await restart_stuck(maker, runtime, REGISTRY, world.org_id) == 0
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_messages SET updated_at = now() - interval '10 minutes' WHERE id = :m",
        {"m": draft_id},
    )
    assert await restart_stuck(maker, runtime, REGISTRY, world.org_id) == 1
    after = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert after["status"] == "awaiting_approval"
    assert after["approval_id"]


async def test_candidate_replies_land_in_the_conversation(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    from pairly_core.errors import BadRequestError
    from pairly_hire.messaging.replies import record_reply

    api = hire_api
    job = await open_job(api, world)
    app_id = (await apply_many(api, world, job, ["Reply Person"]))["Reply Person"]
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        with pytest.raises(BadRequestError):
            await record_reply(session, uuid.UUID(app_id), text="   ")
        await record_reply(
            session,
            uuid.UUID(app_id),
            text="Thank you, Tuesday works for me.",
            subject="Re: interview",
        )
    profile = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    ).json()
    (reply,) = [m for m in profile["messages"] if m["status"] == "received"]
    assert reply["text"] == "Thank you, Tuesday works for me."
    assert reply["purpose"] == "reply"
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.message.received'",
    )
    assert "Tuesday" not in json.dumps([e.payload for e in events])

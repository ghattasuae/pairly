# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The candidate portal (WP2.9) on Postgres, through the API.

* Sign-in: the acknowledgement email's magic link opens a portal session once; a link
  asked from the portal comes by email only for a known address (the answer is the same
  either way); expired, reused, tampered and other-organisation links fail alike; the
  portal session never works on staff routes and staff credentials never work on portal
  routes; sign-out ends the session.
* Isolation: one candidate never reaches another's applications, messages, files or
  decisions, in the same organisation or another; the restrictive Row Level Security
  layer (portal subject) shows a portal session nothing of another candidate and
  nothing reviewers wrote.
* Timeline: honest steps only; never notes, tags, scores, internal stage names or a
  rejection that was not communicated yet.
* Withdraw (confirmed, audited as the candidate, confirmation email), replies, requested
  files, data export (own data only), deletion requests (for admins, nothing deleted)
  and human review requests by decision id (the engine's appeals, candidate receipt).
"""

from __future__ import annotations

import json
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.audit import ActorType
from pairly_core.db import create_sessionmaker, set_portal_subject, set_tenant
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, PromptRef, record_decision
from pairly_core.policy_versions import get_policy
from pairly_hire.models import HireApplication
from pairly_hire.pipeline.models import HireNote
from pairly_hire.portal.auth import portal_subject
from pairly_hire.rubric import parse_rubric

from .hire_support import HireApi, HireWorld, cv_pdf, hire_api, open_job, owner_rows
from .test_hire_pipeline import _key, deliver, move, other, world

__all__ = ["hire_api", "other", "world"]  # fixtures

HEADER = "X-Pairly-Portal-Session"
LINK_RE = re.compile(r"/(en|ar)/portal/login#(pl1\.[A-Za-z0-9._-]+)")


async def _apply(
    api: HireApi, world: HireWorld, job: dict[str, Any], name: str, **kw: Any
) -> tuple[str, str]:
    email = f"{name.lower().replace(' ', '.')}.{uuid.uuid4().hex[:6]}@example.test"
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf(uuid.uuid4().hex),
        email=email,
        full_name=name,
        ip=f"198.51.100.{uuid.uuid4().int % 200 + 20}",
        **kw,
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    (found,) = [a for a in listed.json()["items"] if a["reference"] == reference]
    return str(found["id"]), email


def _link(mails: list[dict[str, Any]], email: str) -> str:
    for mail in reversed(mails):
        if email in mail["to"]:
            found = LINK_RE.search(mail["text"])
            if found:
                return found.group(2)
    raise AssertionError(f"no portal link for {email}")


async def _session(api: HireApi, token: str) -> httpx.Response:
    return await api.client.post(
        "/v1/hire/portal/session", json={"token": token}, headers=api.public()
    )


async def _signed_in(api: HireApi, world: HireWorld, email: str, outbox: Path) -> dict[str, str]:
    token = _link(await deliver(api, world, outbox), email)
    opened = await _session(api, token)
    assert opened.status_code == 200, opened.text
    return {HEADER: opened.json()["session_token"]}


async def test_magic_link_signs_in_once_and_never_works_as_staff(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Mona Said")
    mails = await deliver(api, world, tmp_path)
    ack = next(m for m in mails if email in m["to"])
    assert "/en/portal/login#pl1." in ack["text"]
    token = _link(mails, email)

    opened = await _session(api, token)
    assert opened.status_code == 200, opened.text
    session = {HEADER: opened.json()["session_token"]}
    assert opened.headers["cache-control"] == "no-store"
    # Single use.
    assert (await _session(api, token)).status_code == 404

    me = await api.client.get("/v1/hire/portal/me", headers=session)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
    assert me.json()["full_name"] == "Mona Said"
    apps = (await api.client.get("/v1/hire/portal/applications", headers=session)).json()
    assert [a["id"] for a in apps] == [app_id]
    assert apps[0]["status"] == "received"

    # A portal session is no staff credential, in either header.
    bearer = {"Authorization": f"Bearer {session[HEADER]}", "X-Pairly-Org": str(world.org_id)}
    for headers in (bearer, {**session, "X-Pairly-Org": str(world.org_id)}):
        staff = await api.client.get(f"/v1/hire/applications/{app_id}", headers=headers)
        assert staff.status_code == 401, staff.text
    # A staff credential does nothing on the portal.
    staff_on_portal = await api.client.get(
        "/v1/hire/portal/applications", headers=api.headers(world, "recruiter")
    )
    assert staff_on_portal.status_code == 401
    garbled = await api.client.get(
        "/v1/hire/portal/applications", headers={HEADER: session[HEADER][:-2] + "xx"}
    )
    assert garbled.status_code == 401

    out = await api.client.post("/v1/hire/portal/sign-out", headers=session)
    assert out.status_code == 204
    assert (await api.client.get("/v1/hire/portal/me", headers=session)).status_code == 401


async def test_login_links_expire_are_bound_and_never_tell_who_applied(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    _, email = await _apply(api, world, job, "Rami Haddad")
    _, second = await _apply(api, world, job, "Sara Nasser")
    await deliver(api, world, tmp_path)  # acknowledgements

    async def ask(address: str, slug: str = world.slug) -> httpx.Response:
        return await api.client.post(
            "/v1/hire/portal/login-link",
            json={"org": slug, "email": address, "locale": "ar"},
            headers=api.public("203.0.113.9"),
        )

    known, unknown = await ask(email.upper()), await ask("nobody@example.test")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"sent": True}
    mails = await deliver(api, world, tmp_path)
    links = [m for m in mails if email in m["to"] and "/ar/portal/login#" in m["text"]]
    assert len(links) == 1
    assert "nobody@example.test" not in json.dumps(mails)
    token = _link(mails, email)

    # Bound to its candidate and organisation: changing either part fails.
    parts = token.split(".")
    others = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT candidate_id FROM hire_applications WHERE org_id = :o",
        {"o": world.org_id},
    )
    someone_else = next(r.candidate_id for r in others if r.candidate_id.hex != parts[3])
    for forged in (
        ".".join([parts[0], parts[1], parts[2], someone_else.hex, parts[4]]),
        ".".join([parts[0], other.org_id.hex, *parts[2:]]),
        ".".join([*parts[:4], parts[4][:-1] + ("A" if parts[4][-1] != "A" else "B")]),
    ):
        assert (await _session(api, forged)).status_code == 404
    # Expired: the same answer.
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_portal_tokens SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '1 minute' WHERE id = :i",
        {"i": uuid.UUID(hex=parts[2])},
    )
    assert (await _session(api, token)).status_code == 404
    # The other organisation's careers page does not know this candidate.
    assert (await ask(email, other.slug)).status_code == 202
    assert not [m for m in await deliver(api, other, tmp_path / "other") if email in m["to"]]
    # By organisation id (what an expired link names): the second candidate's new link
    # signs them in, as themselves only.
    assert (await ask(second, world.org_id.hex)).status_code == 202
    session = await _signed_in(api, world, second, tmp_path)
    me = (await api.client.get("/v1/hire/portal/me", headers=session)).json()
    assert me["email"] == second


async def test_candidates_never_reach_each_other(
    hire_api: HireApi, world: HireWorld, other: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    mine, email = await _apply(api, world, job, "Huda Karim", self_id={"gender": "woman"})
    theirs, _ = await _apply(api, world, job, "Omar Saleh", self_id={"gender": "man"})
    foreign_job = await open_job(api, other)
    foreign, _ = await _apply(api, other, foreign_job, "Lina Aziz")
    note = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{mine}/notes",
        world,
        "recruiter",
        json={"body": "Private reviewer note about Huda"},
    )
    assert note.status_code == 201
    asked = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{theirs}/document-requests",
        world,
        "recruiter",
        json={"label": "Portfolio"},
    )
    assert asked.status_code == 201, asked.text
    session = await _signed_in(api, world, email, tmp_path)

    for app_id in (theirs, foreign):
        detail = await api.client.get(f"/v1/hire/portal/applications/{app_id}", headers=session)
        assert detail.status_code == 404
        replied = await api.client.post(
            f"/v1/hire/portal/applications/{app_id}/messages",
            json={"text": "hello"},
            headers=session,
        )
        assert replied.status_code == 404
        withdrawn = await api.client.post(
            f"/v1/hire/portal/applications/{app_id}/withdraw",
            json={"confirm": True},
            headers=session,
        )
        assert withdrawn.status_code == 404
    upload = await api.client.post(
        f"/v1/hire/portal/document-requests/{asked.json()['id']}/file",
        files={"file": ("p.pdf", cv_pdf("x"), "application/pdf")},
        headers=session,
    )
    assert upload.status_code == 404

    # The database itself, as a portal session of this candidate (app role, both RLS
    # layers): their own application only, and none of what reviewers wrote.
    db = create_sessionmaker(api.engine)()
    set_tenant(db, world.org_id, None)
    async with db, db.begin():
        own = (
            await db.execute(
                select(HireApplication.candidate_id).where(HireApplication.id == uuid.UUID(mine))
            )
        ).scalar_one()
    db = create_sessionmaker(api.engine)()
    set_tenant(db, world.org_id, None)
    set_portal_subject(db, portal_subject(own))
    async with db, db.begin():
        visible = list(await db.scalars(select(HireApplication.id)))
        notes = list(await db.scalars(select(HireNote.id)))
        raw = (await db.execute(text("SELECT count(*) FROM hire_screenings"))).scalar_one()
        self_id = list(
            await db.execute(text("SELECT application_id FROM hire_self_identification"))
        )
    assert [r.application_id for r in self_id] == [uuid.UUID(mine)]
    # Read only: a portal session never writes self-identification answers.
    db = create_sessionmaker(api.engine)()
    set_tenant(db, world.org_id, None)
    set_portal_subject(db, portal_subject(own))
    with pytest.raises(Exception, match="row-level security"):
        async with db, db.begin():
            await db.execute(
                text(
                    "INSERT INTO hire_self_identification (org_id, application_id, job_id, "
                    "answers) SELECT org_id, id, job_id, answers FROM hire_applications "
                    "WHERE id = :a"
                ),
                {"a": mine},
            )
    assert visible == [uuid.UUID(mine)]
    assert notes == []
    assert raw == 0
    # Staff (no portal subject) still see both, and the note.
    db = create_sessionmaker(api.engine)()
    set_tenant(db, world.org_id, None)
    async with db, db.begin():
        assert len(list(await db.scalars(select(HireApplication.id)))) == 2
        assert len(list(await db.scalars(select(HireNote.id)))) == 1


async def test_timeline_is_honest_and_never_leaks_internal_data(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    stages = [
        {"key": "applied", "label": "New"},
        {"key": "phone_screen", "label": "Weak python, check salary"},
        {"key": "shortlist", "label": "Top 3", "reveal_identity": True},
        {"key": "interview", "label": "Panel"},
        {"key": "offer", "label": "Offer"},
        {"key": "hired", "label": "Hired", "kind": "hired"},
        {"key": "rejected", "label": "No", "kind": "rejected"},
        {"key": "withdrawn", "label": "Gone", "kind": "withdrawn"},
    ]
    saved = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/stages", world, "recruiter", json=stages
    )
    assert saved.status_code == 200, saved.text
    app_id, email = await _apply(api, world, job, "Nadia Fares")
    await api.call(
        "PUT",
        f"/v1/hire/pipeline/applications/{app_id}/tags",
        world,
        "recruiter",
        json={"tags": ["salary-risk"]},
    )
    await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/notes",
        world,
        "recruiter",
        json={"body": "Secret note: not a culture fit"},
    )
    session = await _signed_in(api, world, email, tmp_path)

    async def mine() -> dict[str, Any]:
        response = await api.client.get(f"/v1/hire/portal/applications/{app_id}", headers=session)
        assert response.status_code == 200, response.text
        return dict(response.json())

    assert (await move(api, world, app_id, "phone_screen")).status_code == 200
    seen = await mine()
    assert seen["status"] == "in_review"
    assert (await move(api, world, app_id, "shortlist")).status_code == 200
    assert (await mine())["status"] == "moving_forward"
    # A slip undone never shows.
    slipped = await move(api, world, app_id, "offer")
    undo = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [slipped.json()["move_id"]]},
    )
    assert undo.status_code == 200, undo.text
    # A rejection is not shown until its message, approved by a person, was sent.
    rejected = await move(api, world, app_id, "rejected")
    draft_id = rejected.json()["draft_id"]
    seen = await mine()
    assert seen["status"] == "moving_forward"
    assert [h["step"] for h in seen["history"]] == ["received", "in_review", "moving_forward"]
    dumped = json.dumps(seen)
    for secret in (
        "Weak python",
        "phone_screen",
        "Top 3",
        "salary-risk",
        "Secret note",
        "culture",
        "score",
        "overall",
        "rejected",
        "flag",
        "review_reasons",
        "recruiter",
    ):
        assert secret not in dumped, secret
    submitted = await api.client.post(
        f"/v1/hire/messages/{draft_id}/submit",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert submitted.status_code == 200, submitted.text
    assert (await mine())["status"] == "moving_forward"
    approval_id = (
        await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")
    ).json()["approval_id"]
    approved = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert approved.status_code == 200, approved.text
    seen = await mine()
    assert seen["status"] == "not_selected"
    assert seen["final"] is True
    assert seen["can_withdraw"] is False
    # The sent rejection is in their messages; drafts never were.
    assert any("not to move forward" in m["text"] for m in seen["messages"])
    assert all(m["direction"] in {"from_company", "from_you"} for m in seen["messages"])


async def test_withdraw_reply_and_requested_files(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Karim Odeh")
    session = await _signed_in(api, world, email, tmp_path)

    # A reply reaches the hiring team's timeline.
    replied = await api.client.post(
        f"/v1/hire/portal/applications/{app_id}/messages",
        json={"text": "Happy to share more examples of my work."},
        headers=session,
    )
    assert replied.status_code == 201, replied.text
    assert replied.json()["direction"] == "from_you"
    profile = (
        await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    ).json()
    assert any(m["purpose"] == "reply" for m in profile["messages"])

    # A requested file, uploaded through the portal.
    asked = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/document-requests",
        world,
        "recruiter",
        json={"label": "Portfolio", "label_ar": "ملف الأعمال"},
    )
    assert asked.status_code == 201, asked.text
    detail = (
        await api.client.get(f"/v1/hire/portal/applications/{app_id}", headers=session)
    ).json()
    assert detail["open_document_requests"] == 1
    assert detail["document_requests"][0]["label"] == "Portfolio"
    sent = await api.client.post(
        f"/v1/hire/portal/document-requests/{asked.json()['id']}/file",
        files={"file": ("portfolio.pdf", cv_pdf("portfolio"), "application/pdf")},
        headers=session,
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["request"]["status"] == "received"
    again = await api.client.post(
        f"/v1/hire/portal/document-requests/{asked.json()['id']}/file",
        files={"file": ("portfolio.pdf", cv_pdf("portfolio2"), "application/pdf")},
        headers=session,
    )
    assert again.status_code == 409
    staff = await api.call(
        "GET", f"/v1/hire/pipeline/applications/{app_id}/document-requests", world, "recruiter"
    )
    # Blind: the uploaded file's id is not shown to reviewers yet.
    assert staff.json()[0]["status"] == "received"
    assert staff.json()[0]["document_id"] is None

    # Withdraw: confirmation required, audited as the candidate, an email.
    draft = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/messages",
        world,
        "recruiter",
        json={"template_key": "hire.candidate.status_update"},
    )
    assert draft.status_code == 201
    unconfirmed = await api.client.post(
        f"/v1/hire/portal/applications/{app_id}/withdraw", json={}, headers=session
    )
    assert unconfirmed.status_code == 422
    done = await api.client.post(
        f"/v1/hire/portal/applications/{app_id}/withdraw",
        json={"confirm": True},
        headers=session,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "withdrawn"
    assert done.json()["can_withdraw"] is False
    twice = await api.client.post(
        f"/v1/hire/portal/applications/{app_id}/withdraw",
        json={"confirm": True},
        headers=session,
    )
    assert twice.status_code == 409
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM hire_applications WHERE id = :a",
        {"a": app_id},
    )
    assert row.status == "withdrawn"
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action, actor_type, actor_id FROM audit_events WHERE entity_id = :a "
        "AND action IN ('hire.application.withdrawn', 'hire.candidate.moved')",
        {"a": app_id},
    )
    assert {(e.action, e.actor_type) for e in events} == {
        ("hire.application.withdrawn", "candidate"),
        ("hire.candidate.moved", "candidate"),
    }
    (moved,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_type, to_stage FROM hire_stage_moves WHERE application_id = :a",
        {"a": app_id},
    )
    assert (moved.actor_type, moved.to_stage) == ("candidate", "withdrawn")
    status = (
        await api.call("GET", f"/v1/hire/messages/{draft.json()['id']}", world, "recruiter")
    ).json()
    assert status["status"] == "cancelled"
    mails = await deliver(api, world, tmp_path)
    confirmation = [m for m in mails if email in m["to"] and "withdrew" in m["subject"]]
    assert len(confirmation) == 1
    assert "Karim Odeh" in confirmation[0]["text"]
    assert "\u2014" not in confirmation[0]["text"]


async def test_privacy_centre_export_deletion_and_human_review(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(
        api, world, job, "Tala Mansour", self_id={"gender": "woman", "age_range": "25_34"}
    )
    other_app, other_email = await _apply(
        api, world, job, "Yara Daher", self_id={"disability": "yes"}
    )
    await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/notes",
        world,
        "recruiter",
        json={"body": "Reviewer only: strong systems design"},
    )
    await api.call(
        "PUT",
        f"/v1/hire/pipeline/applications/{app_id}/tags",
        world,
        "recruiter",
        json={"tags": ["fast-track"]},
    )
    # One AI-assisted decision about each application (a score record, as screening
    # writes it: the job's rubric version, quotes, model and prompt metadata, flags).
    ids: dict[str, str] = {}
    db = create_sessionmaker(owner_engine)()
    set_tenant(db, world.org_id, None)
    async with db, db.begin():
        rubric_id = (
            await db.execute(
                text("SELECT rubric_policy_id FROM hire_jobs WHERE id = :j"), {"j": job["id"]}
            )
        ).scalar_one()
        policy = await get_policy(db, rubric_id)
        assert policy is not None
        criterion = parse_rubric(policy.content).criteria[0]
        for app in (app_id, other_app):
            stored = await record_decision(
                db,
                DecisionDraft(
                    kind=DecisionKind.SCORE,
                    workflow="hire",
                    entity_type="hire.application",
                    entity_id=app,
                    actor_type=ActorType.SYSTEM,
                    actor_id="test.screening",
                    rubric_version_id=rubric_id,
                    prompt_versions=[PromptRef("hire_score", "v3", "prompt-hash-secret")],
                    model={"provider": "anthropic", "model": "claude-x", "tier": "high"},
                    output={"scores": {criterion.id: 3}, "overall": 70.0},
                    evidence={
                        "criteria": [{"criterion_id": criterion.id, "model_score": 3}],
                        "flags": [{"code": "unexplained_gap", "kind": "claim"}],
                    },
                    sensitive={
                        "evidence": {criterion.id: ["built payment APIs in Python"]},
                        "why_not_higher": {criterion.id: "Generated: no lead role"},
                        "reason": "Internal note: pricey",
                    },
                    confidence=0.9,
                ),
            )
            ids[app] = stored.id
    session = await _signed_in(api, world, email, tmp_path)

    export = await api.client.get("/v1/hire/portal/privacy/export", headers=session)
    assert export.status_code == 200, export.text
    assert "attachment" in export.headers["content-disposition"]
    data = export.json()
    dumped = json.dumps(data, ensure_ascii=False)
    assert data["candidate"]["email"] == email
    assert [a["status"] for a in data["applications"]] == ["received"]
    assert data["decisions"][0]["decision_id"] == ids[app_id]
    for secret in (
        other_email,
        "Yara",
        ids[other_app],
        "Reviewer only",
        "fast-track",
        '"overall"',
        "python",
        "disability",
    ):
        assert secret not in dumped, secret
    # The candidate's own self-identification answers, as they gave them (and only theirs).
    assert data["applications"][0]["self_identification"] == [
        {
            "category": "age_range",
            "question": "Age range",
            "answer": "25_34",
            "answer_label": "25 to 34",
        },
        {"category": "gender", "question": "Gender", "answer": "woman", "answer_label": "Woman"},
    ]
    assert "the hiring team never sees them" in data["not_included"]

    deletion = await api.client.post(
        "/v1/hire/portal/privacy/deletion-request",
        json={"note": "Please delete my CV."},
        headers=session,
    )
    assert deletion.status_code == 201, deletion.text
    assert deletion.json()["request"]["status"] == "open"
    repeat = await api.client.post(
        "/v1/hire/portal/privacy/deletion-request", json={}, headers=session
    )
    assert repeat.status_code == 200
    assert repeat.json()["created"] is False
    listed = await api.call("GET", "/v1/hire/data-requests", world, "admin")
    assert listed.status_code == 200, listed.text
    (request,) = listed.json()
    assert request["candidate_email"] == email
    assert request["note"] == "Please delete my CV."
    assert (await api.call("GET", "/v1/hire/data-requests", world, "recruiter")).status_code == 403
    resolved = await api.call(
        "POST",
        f"/v1/hire/data-requests/{request['id']}/resolve",
        world,
        "admin",
        json={"status": "completed", "outcome": "Deleted the CV by hand after checking."},
    )
    assert resolved.status_code == 200, resolved.text
    # Nothing was deleted by the request itself.
    (still,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM hire_applications WHERE id = :a",
        {"a": app_id},
    )
    assert still.n == 1

    privacy = (await api.client.get("/v1/hire/portal/privacy", headers=session)).json()
    (decision,) = privacy["decisions"]
    assert decision["decision_id"] == ids[app_id]
    assert decision["can_appeal"] is True
    receipt = await api.client.get(
        f"/v1/hire/portal/privacy/decisions/{ids[app_id]}", headers=session
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["audience"] == "subject"
    shown = receipt.json()
    assert "actor" not in shown
    assert "model" not in shown
    # Per criterion: the outcome, the rubric's anchor text and the candidate's own quotes;
    # the model and prompt version only; nothing internal.
    assert shown["criteria"] == [
        {
            "criterion_id": criterion.id,
            "name": criterion.name,
            "score": 3,
            "scale_max": 4,
            "anchor": criterion.scale_anchors[3],
            "your_evidence": ["built payment APIs in Python"],
        }
    ]
    assert shown["versions"] == {
        "model": "claude-x",
        "prompts": [{"task": "hire_score", "version": "v3"}],
    }
    dumped_receipt = json.dumps(shown)
    for hidden in (
        "anthropic",
        "prompt-hash-secret",
        "unexplained_gap",
        "Generated:",
        "Internal note",
        "model_score",
        other_email,
        ids[other_app],
    ):
        assert hidden not in dumped_receipt, hidden
    foreign = await api.client.get(
        f"/v1/hire/portal/privacy/decisions/{ids[other_app]}", headers=session
    )
    assert foreign.status_code == 404

    appealed = await api.client.post(
        "/v1/hire/portal/privacy/appeals",
        json={"decision_id": ids[app_id], "reason": "Please have a person read my CV again."},
        headers=session,
    )
    assert appealed.status_code == 201, appealed.text
    assert appealed.json()["status"] == "open"
    twice = await api.client.post(
        "/v1/hire/portal/privacy/appeals",
        json={"decision_id": ids[app_id], "reason": "Please have a person read my CV again."},
        headers=session,
    )
    assert twice.status_code == 409
    not_mine = await api.client.post(
        "/v1/hire/portal/privacy/appeals",
        json={"decision_id": ids[other_app], "reason": "Please have a person read this one."},
        headers=session,
    )
    assert not_mine.status_code == 404
    (appeal,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT requester_type, requester_id, filed_by FROM appeals WHERE decision_id = :d",
        {"d": ids[app_id]},
    )
    assert appeal.requester_type == "candidate"
    assert appeal.filed_by is None
    # The hiring team sees it in the appeals queue of the Hire workflow.
    queue = await api.call("GET", "/v1/appeals", world, "recruiter")
    assert queue.status_code == 200, queue.text
    assert [a["decision_id"] for a in queue.json()] == [ids[app_id]]
    created = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_type FROM audit_events WHERE action = 'core.appeal.created'",
    )
    assert [c.actor_type for c in created] == ["candidate"]


async def test_admin_erases_a_candidate_only_by_hand_with_a_reason(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Rami Haddad")
    _, other_email = await _apply(api, world, job, "Lina Saleh")
    session = await _signed_in(api, world, email, tmp_path)
    asked = await api.client.post(
        "/v1/hire/portal/privacy/deletion-request", json={}, headers=session
    )
    assert asked.status_code == 201, asked.text
    request_id = asked.json()["request"]["id"]
    one = await api.call("GET", f"/v1/hire/data-requests/{request_id}", world, "admin")
    assert one.status_code == 200, one.text
    assert one.json()["candidate_email"] == email
    assert one.json()["due_at"] > one.json()["created_at"]
    path = f"/v1/hire/data-requests/{request_id}/erase"
    reason = {"reason": "Candidate asked by email to delete everything."}

    # Admins only, with a second factor, and always with a written reason.
    assert (await api.call("POST", path, world, "recruiter", json=reason)).status_code == 403
    no_otp = await api.call("POST", path, world, "admin", otp=False, json=reason)
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"
    short = await api.call("POST", path, world, "admin", json={"reason": "ok"})
    assert short.status_code == 422
    # Asking deletes nothing.
    listed = await api.call("GET", "/v1/hire/data-requests", world, "admin")
    assert {r["candidate_email"] for r in listed.json()} == {email}

    erased = await api.call("POST", path, world, "admin", json=reason)
    assert erased.status_code == 200, erased.text
    done = erased.json()
    assert done["status"] == "completed"
    assert done["outcome"] == reason["reason"]
    assert email not in erased.text
    assert done["candidate_name"] == "Erased candidate"
    again = await api.call("POST", path, world, "admin", json=reason)
    assert again.status_code == 409

    profile = await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    assert profile.status_code == 200, profile.text
    assert "Rami" not in profile.text
    assert email not in profile.text
    # The other candidate is untouched; the application row itself stays.
    others = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "admin")
    assert len(others.json()["items"]) == 2
    assert other_email
    started = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.data_request.erasure_started'",
    )
    (audit,) = started
    assert audit.payload["applications"] == 1
    assert reason["reason"] not in str(audit.payload)
    (completed,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.data_request.erased'",
    )
    assert completed.payload["documents_removed"] == 1
    assert reason["reason"] not in str(completed.payload)


async def test_portal_shows_upcoming_interviews_with_their_links(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    """Interviews come from scheduling (WP2.8): a booked one with its join link and the
    candidate's change-or-cancel link, a proposed one with the link to choose a time;
    never the panel, and never another candidate's interview."""
    api = hire_api
    job = await open_job(api, world)
    booked_app, email = await _apply(api, world, job, "Rana Haddad")
    proposed_job = await open_job(api, world)
    proposed_app, _ = await api_apply_same(api, world, proposed_job, email)
    other_app, _ = await _apply(api, world, job, "Sami Nasser")
    panel = [str(world.people["interviewer"][0])]

    def key() -> dict[str, str]:
        return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}

    found = await api.call(
        "POST",
        f"/v1/hire/scheduling/applications/{booked_app}/slots",
        world,
        "recruiter",
        json={"panel": panel, "duration_min": 45},
    )
    assert found.status_code == 200, found.text
    start = found.json()["slots"][0]["start"]
    booked = await api.client.post(
        f"/v1/hire/scheduling/applications/{booked_app}/interviews",
        json={"mode": "book", "panel": panel, "duration_min": 45, "starts_at": start},
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert booked.status_code == 201, booked.text
    for app_id in (proposed_app, other_app):
        linked = await api.client.post(
            f"/v1/hire/scheduling/applications/{app_id}/interviews",
            json={"mode": "link", "panel": panel, "duration_min": 30},
            headers=api.headers(world, "recruiter", extra=key()),
        )
        assert linked.status_code == 201, linked.text

    session = await _signed_in(api, world, email, tmp_path)
    listed = (await api.client.get("/v1/hire/portal/applications", headers=session)).json()
    by_id = {a["id"]: a for a in listed}
    assert set(by_id) == {booked_app, proposed_app}
    (iv,) = by_id[booked_app]["interviews"]
    assert iv["id"] == booked.json()["id"]
    assert iv["status"] == "booked"
    assert iv["starts_at"] is not None
    assert str(iv["join_url"]).startswith("https://")
    assert "/schedule#hs1." in str(iv["reschedule_url"])
    assert iv["choose_url"] is None
    (pending,) = by_id[proposed_app]["interviews"]
    assert pending["status"] == "proposed"
    assert pending["starts_at"] is None
    assert pending["duration_min"] == 30
    assert "/schedule#hs1." in str(pending["choose_url"])
    assert pending["join_url"] is None
    detail = await api.client.get(f"/v1/hire/portal/applications/{booked_app}", headers=session)
    assert detail.status_code == 200, detail.text
    assert [i["id"] for i in detail.json()["interviews"]] == [iv["id"]]
    # Nothing about the panel reaches the candidate.
    text_ = json.dumps(listed)
    assert panel[0] not in text_
    assert "panel" not in text_


async def api_apply_same(
    api: HireApi, world: HireWorld, job: dict[str, Any], email: str
) -> tuple[str, str]:
    """A second application by the same person (same email, so the same candidate)."""
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf(uuid.uuid4().hex),
        email=email,
        full_name="Rana Haddad",
        ip=f"198.51.100.{uuid.uuid4().int % 200 + 20}",
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    (found,) = [a for a in listed.json()["items"] if a["reference"] == reference]
    return str(found["id"]), email


async def _notices(owner: AsyncEngine, world: HireWorld, template: str) -> list[Any]:
    return await owner_rows(
        owner,
        world.org_id,
        "SELECT recipient_user_id, channel, category, urgency, status, dedupe_key "
        "FROM notifications WHERE org_id = :o AND template_key = :t ORDER BY created_at",
        {"o": world.org_id, "t": template},
    )


async def test_notices_for_admins_recruiters_and_the_candidate(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Data requests go to Hire admins (in the app and by email, urgent, with a deadline);
    replies and files to the job's recruiter (digest-friendly updates); a file request is
    emailed to the candidate. No notice names the candidate."""
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Dana Khalil")
    admin = world.people["admin"][0]
    recruiter = world.people["recruiter"][0]
    db = create_sessionmaker(owner_engine)()
    set_tenant(db, world.org_id, None)
    async with db, db.begin():
        stored = await record_decision(
            db,
            DecisionDraft(
                kind=DecisionKind.SCORE,
                workflow="hire",
                entity_type="hire.application",
                entity_id=app_id,
                actor_type=ActorType.SYSTEM,
                actor_id="test.screening",
                output={"overall": 60.0},
                confidence=0.9,
            ),
        )
    session = await _signed_in(api, world, email, tmp_path)

    # A file request: the candidate is emailed, with the way into the portal.
    asked = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/document-requests",
        world,
        "recruiter",
        json={"label": "Portfolio"},
    )
    assert asked.status_code == 201, asked.text
    mails = await deliver(api, world, tmp_path / "files")
    (mail,) = [m for m in mails if email in m["to"] and "asked you for a file" in m["subject"]]
    assert "Portfolio" in mail["subject"]
    assert "/en/portal/login" in mail["text"]

    # Deletion, human review and export: urgent notices to each Hire admin.
    assert (
        await api.client.post("/v1/hire/portal/privacy/deletion-request", json={}, headers=session)
    ).status_code == 201
    assert (
        await api.client.post(
            "/v1/hire/portal/privacy/appeals",
            json={"decision_id": stored.id, "reason": "Please have a person look again."},
            headers=session,
        )
    ).status_code == 201
    assert (
        await api.client.get("/v1/hire/portal/privacy/export", headers=session)
    ).status_code == 200
    rows = await _notices(owner_engine, world, "hire.portal.data_request")
    assert {r.recipient_user_id for r in rows} == {admin}
    assert {r.channel for r in rows} == {"in_app", "email"}
    assert {r.category for r in rows} == {"data_requests"}
    assert {r.urgency for r in rows} == {"urgent"}
    kinds = sorted({r.dedupe_key.split(":")[1] for r in rows})
    assert kinds == ["deletion", "export", "human_review"]
    assert len(rows) == 6

    # A reply and an uploaded file: the job's recruiter (who also asked for the file).
    replied = await api.client.post(
        f"/v1/hire/portal/applications/{app_id}/messages",
        json={"text": "Thank you, happy to talk."},
        headers=session,
    )
    assert replied.status_code in (200, 201), replied.text
    uploaded = await api.client.post(
        f"/v1/hire/portal/document-requests/{asked.json()['id']}/file",
        files={"file": ("portfolio.pdf", cv_pdf("portfolio"), "application/pdf")},
        headers=session,
    )
    assert uploaded.status_code in (200, 201), uploaded.text
    rows = await _notices(owner_engine, world, "hire.portal.candidate_activity")
    assert {r.recipient_user_id for r in rows} == {recruiter}
    assert {r.category for r in rows} == {"updates"}
    assert sorted({r.dedupe_key.split(":")[1] for r in rows}) == ["file", "reply"]

    # Nothing names the candidate: the in-app items and emails to staff.
    mails = await deliver(api, world, tmp_path / "staff")
    staff = [m for m in mails if email not in m["to"]]
    assert staff
    for m in staff:
        assert "Dana" not in m["subject"] + m["text"]


async def test_login_link_answers_known_and_unknown_addresses_in_the_same_time(
    hire_api: HireApi, world: HireWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request does the same work for any address (hash it, schedule the sender);
    only the sender, after the answer, looks the candidate up. Timed with the sender
    recorded, then the real sender is run for both jobs."""
    from pairly_hire.api import portal as portal_api

    api = hire_api
    job = await open_job(api, world)
    _, email = await _apply(api, world, job, "Rima Saad")
    await deliver(api, world, tmp_path / "ack")
    jobs: list[Any] = []

    async def record(sessionmaker: Any, link_job: Any) -> None:
        jobs.append((sessionmaker, link_job))

    monkeypatch.setattr(portal_api, "LINK_SENDER", [record])

    async def ask(address: str, n: int) -> float:
        # Timing, not rate limits: a fresh limiter store for each request.
        if hasattr(api.app.state, "hire_portal_limiters"):
            del api.app.state.hire_portal_limiters
        started = time.perf_counter()
        answer = await api.client.post(
            "/v1/hire/portal/login-link",
            json={"org": world.slug, "email": address, "locale": "en"},
            headers=api.public(f"203.0.113.{n % 200 + 20}"),
        )
        elapsed = time.perf_counter() - started
        assert answer.status_code == 202
        assert answer.json() == {"sent": True}
        return elapsed

    for n in range(4):  # warm up
        await ask(email, n)
        await ask(f"nobody{n}@example.test", n)
    known: list[float] = []
    unknown: list[float] = []
    for n in range(30):
        known.append(await ask(email, n))
        unknown.append(await ask(f"nobody{n}@example.test", n))
    k, u = statistics.median(known), statistics.median(unknown)
    print(f"login-link median: known {k * 1000:.2f} ms, unknown {u * 1000:.2f} ms")
    # Same work: medians within 5 ms or 35 % of each other (CI noise), whichever is larger.
    assert abs(k - u) <= max(0.005, 0.35 * max(k, u)), (k, u)
    # Both paths scheduled the sender with the same kind of job.
    assert len(jobs) == 68
    assert {type(j) for _, j in jobs} == {portal_api.LinkJob}

    # The real sender: one email for the known address, nothing for an unknown one.
    sessionmaker, known_job = jobs[-2]
    _, unknown_job = jobs[-1]
    await portal_api.send_login_link(sessionmaker, known_job)
    await portal_api.send_login_link(sessionmaker, unknown_job)
    mails = await deliver(api, world, tmp_path / "links")
    assert len([m for m in mails if email in m["to"] and "/en/portal/login#" in m["text"]]) == 1
    assert "nobody" not in json.dumps(mails)


async def test_link_lifetimes_30_minutes_from_the_portal_72_hours_from_the_acknowledgement(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    _, email = await _apply(api, world, job, "Lina Farah")
    ack_token = _link(await deliver(api, world, tmp_path), email)
    asked = await api.client.post(
        "/v1/hire/portal/login-link",
        json={"org": world.slug, "email": email, "locale": "en"},
        headers=api.public("203.0.113.77"),
    )
    assert asked.status_code == 202
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT purpose, extract(epoch FROM expires_at - created_at) AS seconds, used_at "
        "FROM hire_portal_tokens WHERE org_id = :o ORDER BY created_at",
        {"o": world.org_id},
    )
    lifetimes = {r.purpose: int(r.seconds) for r in rows}
    assert lifetimes["request"] == 30 * 60
    (ack,) = [p for p in lifetimes if p != "request"]
    assert lifetimes[ack] == 72 * 3600
    assert all(r.used_at is None for r in rows)
    # Single use: the acknowledgement link opens one session, then never again.
    assert (await _session(api, ack_token)).status_code == 200
    assert (await _session(api, ack_token)).status_code == 404

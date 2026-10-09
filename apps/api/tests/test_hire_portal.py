# SPDX-License-Identifier: AGPL-3.0-only
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
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_hire.models import HireApplication
from pairly_hire.pipeline.models import HireNote
from pairly_hire.portal.auth import portal_subject

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
    # One AI-assisted decision about each application (a score record).
    ids: dict[str, str] = {}
    db = create_sessionmaker(owner_engine)()
    set_tenant(db, world.org_id, None)
    async with db, db.begin():
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
                    output={"scores": {"python": 3}, "overall": 70.0},
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
    assert "actor" not in receipt.json()
    assert "model" not in receipt.json()
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

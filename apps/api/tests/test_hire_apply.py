# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire careers pages and the public apply flow on Postgres (WP2.2, WP2.3).

End to end without a sign-in: careers data and feeds, the AI-use consent, bot
protection (the fake Turnstile verifier), disposable email and rate limits, the CV
through the engine's ingestion, duplicate hints, knockouts to a person, the
acknowledgement email in the development outbox, self-identification kept apart, tenant
isolation of every Hire table, and the retention rule.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from defusedxml import ElementTree
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.crypto.types import unseal
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.outbox import OutboxTransport
from pairly_core.retention.service import dry_run_org
from pairly_hire.demo import BACKEND, SUPPORT_LEAD
from pairly_hire.models import HireApplication, HireCandidate
from pairly_hire.notice import CURRENT_VERSION
from pairly_hire.retention import RULE_KEY
from pairly_hire.scoring.inputs import ScoringInput, scoring_input
from pairly_hire.selfid.models import HireSelfIdentification

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
from .notify_support import notify_settings

__all__ = ["hire_api"]  # fixture

HIRE_TABLES = (
    "hire_jobs",
    "hire_rubric_drafts",
    "hire_candidates",
    "hire_applications",
    "hire_self_identification",
    "hire_org_settings",
)


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


# ------------------------------------------------------------------ careers pages


async def test_careers_page_feeds_and_json_ld(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    hidden = await api.client.get(f"/v1/hire/public/careers/{world.slug}", headers=api.public())
    assert hidden.status_code == 404  # no settings and no open job: not visible yet

    job = await open_job(api, world)
    draft = await api.call(
        "POST",
        "/v1/hire/jobs",
        world,
        "recruiter",
        json={
            "title": "Secret role",
            "location": "Dubai",
            "work_model": "onsite",
            "employment_type": "full_time",
        },
    )
    assert draft.status_code == 201

    page = await api.client.get(f"/v1/hire/public/careers/{world.slug}", headers=api.public())
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"].startswith("public, max-age=300")
    body = page.json()
    assert body["organisation"] == {"name": world.name, "slug": world.slug}
    assert [j["slug"] for j in body["jobs"]] == [job["slug"]]
    assert body["jobs"][0]["salary"]["currency"] == "AED"
    assert body["jobs"][0]["url"].endswith(f"/en/careers/{world.slug}/{job['slug']}")

    detail = await api.client.get(
        f"/v1/hire/public/careers/{world.slug}/jobs/{job['slug']}?locale=ar", headers=api.public()
    )
    assert detail.status_code == 200, detail.text
    data = detail.json()
    ld = data["json_ld"]
    assert ld["@type"] == "JobPosting"
    assert ld["hiringOrganization"]["name"] == world.name
    assert ld["baseSalary"]["value"]["minValue"] == float(BACKEND.salary_min)
    assert ld["url"].endswith(f"/ar/careers/{world.slug}/{job['slug']}")
    assert data["notice"]["en"]["version"] == CURRENT_VERSION
    assert data["notice"]["ar"]["title"]
    assert "rubric" not in json.dumps(data).lower() or "must_haves" not in json.dumps(data)
    assert "expected_answer" not in json.dumps(data)

    for slug in ("secret-role", "no-such-job"):
        missing = await api.client.get(
            f"/v1/hire/public/careers/{world.slug}/jobs/{slug}", headers=api.public()
        )
        assert missing.status_code == 404

    feed = await api.client.get(f"/v1/hire/public/careers/{world.slug}/feed.json")
    assert feed.status_code == 200
    assert [j["slug"] for j in feed.json()["jobs"]] == [job["slug"]]
    xml = await api.client.get(f"/v1/hire/public/careers/{world.slug}/feed.xml")
    assert xml.status_code == 200
    assert xml.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(xml.content)
    assert [j.findtext("referencenumber") for j in root.findall("job")] == [job["slug"]]

    # The careers page can be switched off.
    off = await api.call(
        "PUT", "/v1/hire/settings", world, "admin", json={"careers_enabled": False}
    )
    assert off.status_code == 200, off.text
    gone = await api.client.get(f"/v1/hire/public/careers/{world.slug}", headers=api.public())
    assert gone.status_code == 404
    unknown = await api.client.get("/v1/hire/public/careers/NOT_A_SLUG", headers=api.public())
    assert unknown.status_code == 404


async def test_public_reads_are_rate_limited(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    await open_job(api, world)
    path = f"/v1/hire/public/careers/{world.slug}"
    for _ in range(120):
        assert (await api.client.get(path, headers=api.public("203.0.113.9"))).status_code == 200
    limited = await api.client.get(path, headers=api.public("203.0.113.9"))
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert (await api.client.get(path, headers=api.public("203.0.113.10"))).status_code == 200


# ------------------------------------------------------------------ apply end to end


async def test_apply_end_to_end(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    started = await api.start(
        world,
        job["slug"],
        self_id={"gender": "woman", "age_range": "25_34", "religion": "x", "disability": "maybe"},
        locale="ar",
    )
    assert started.status_code == 201, started.text
    assert started.headers["cache-control"] == "no-store"
    token = started.json()["token"]
    assert started.json()["required_files"] == ["cv"]

    uploaded = await api.upload(world, token, cv_pdf("layla"))
    assert uploaded.status_code == 201, uploaded.text
    submitted = await api.submit(world, token)
    assert submitted.status_code == 202, submitted.text
    reference = submitted.json()["reference"]
    assert submitted.json() == {"status": "received", "reference": reference, "response_days": 10}

    # The token was single use.
    assert (await api.submit(world, token)).status_code == 404
    assert (await api.upload(world, token, cv_pdf("again"))).status_code == 404

    # Stored: consent with version and time, the CV through ingestion (workflow hire),
    # personal data encrypted, self-identification apart and filtered.
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT a.status, a.consent_version, a.consent_at, a.consent_locale, a.cv_document_id, "
        "a.needs_human_review, a.acknowledged_at, a.answers, c.email, c.full_name, "
        "d.workflow, d.media_type, d.status AS doc_status "
        "FROM hire_applications a JOIN hire_candidates c ON c.id = a.candidate_id "
        "JOIN documents d ON d.id = a.cv_document_id",
    )
    assert row.status == "received"
    assert row.consent_version == CURRENT_VERSION
    assert abs(row.consent_at - datetime.now(UTC)) < timedelta(minutes=5)
    assert row.consent_locale == "ar"
    assert row.workflow == "hire"
    assert row.media_type == "application/pdf"
    assert row.needs_human_review is False
    assert row.acknowledged_at is not None
    for sealed in (row.email, row.full_name, row.answers):
        assert "layla" not in sealed.lower()
        assert "right_to_work" not in sealed
    # The worker scans and parses the CV like every other document.
    assert [(j[0], j[1], j[2]) for j in api.queue.jobs] == [
        ("scan_document", world.org_id, row.cv_document_id)
    ]
    (selfid,) = await owner_rows(
        owner_engine, world.org_id, "SELECT answers FROM hire_self_identification"
    )
    assert "woman" not in selfid.answers  # encrypted
    session = create_sessionmaker(api.engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            stored = await session.scalar(select(HireSelfIdentification))
            assert stored is not None
            assert unseal(stored, "answers") == {"gender": "woman", "age_range": "25_34"}
            app = await session.scalar(select(HireApplication))
            assert app is not None
            view = await scoring_input(session, app.id)
            assert isinstance(view, ScoringInput)
            assert view.knockout_answers == {"right_to_work_uae": "yes"}
            assert view.rubric_version == 1
            assert "gender" not in json.dumps(view.answers)

    # The hiring team sees the application, never the self-identification answers.
    listed = await api.call("GET", "/v1/hire/applications", world, "interviewer")
    (item,) = listed.json()["items"]
    assert item["reference"] == reference
    assert item["candidate_name"] == "Layla Haddad"
    detail = await api.call("GET", f"/v1/hire/applications/{item['id']}", world, "recruiter")
    body = detail.json()
    assert body["email"] == "layla.haddad@example.test"
    assert body["knockouts"][0]["matches"] is True
    assert "woman" not in json.dumps(body)
    assert "gender" not in json.dumps(body)
    assert (await api.call("GET", "/v1/hire/applications", world, "employee")).status_code == 403

    # The acknowledgement email, in Arabic, lands in the development outbox.
    outbox = tmp_path / "outbox"
    stats = await deliver_org(
        create_sessionmaker(api.engine),
        world.org_id,
        Transports(
            settings=notify_settings(),
            email=OutboxTransport(outbox),
            slack=FakeSlackTransport(),
        ),
    )
    assert stats.sent == 1
    (message_file,) = list(outbox.glob("*.json"))
    message = json.loads(message_file.read_text())
    assert message["to"] == ["layla.haddad@example.test"]
    assert BACKEND.draft.title_ar in message["subject"]
    assert reference in message["text"]
    assert "10" in message["text"]
    assert f"/ar/careers/{world.slug}" in message["text"]
    assert "—" not in message["text"]

    audit = [
        r.action
        for r in await owner_rows(
            owner_engine, world.org_id, "SELECT action FROM audit_events ORDER BY seq"
        )
    ]
    for action in (
        "hire.application.started",
        "hire.application.file_added",
        "hire.application.received",
        "core.document.received",
        "core.notification.queued",
    ):
        assert action in audit, action


async def test_consent_bot_check_and_disposable_email(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    cases = [
        ({"consent": False}, "consent_required"),
        ({"notice_version": "2020-01-1"}, "consent_required"),
        ({"turnstile": "not-the-dummy-token"}, "bot_check_failed"),
        ({"email": "someone@mailinator.com"}, "disposable_email"),
        ({"email": "not an email"}, "invalid_email"),
    ]
    for kwargs, code in cases:
        refused = await api.start(world, job["slug"], **kwargs)  # type: ignore[arg-type]
        assert refused.status_code == 400, (kwargs, refused.text)
        assert refused.json()["code"] == code
    # Nothing was stored for any refused attempt.
    session = create_sessionmaker(api.engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            assert await session.scalar(select(func.count()).select_from(HireApplication)) == 0
            assert await session.scalar(select(func.count()).select_from(HireCandidate)) == 0


async def test_rate_limits_per_ip_and_per_email(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    # Per IP: 10 starts an hour, whatever happens to them.
    for _ in range(10):
        refused = await api.start(world, job["slug"], consent=False, ip="203.0.113.50")
        assert refused.status_code == 400
    limited = await api.start(world, job["slug"], ip="203.0.113.50")
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"

    # Per email: 5 a day, from any address.
    for i in range(5):
        ok = await api.start(
            world, job["slug"], email="busy@example.test", ip=f"203.0.113.{60 + i}"
        )
        assert ok.status_code == 201, ok.text
    limited = await api.start(world, job["slug"], email="BUSY@example.test", ip="203.0.113.70")
    assert limited.status_code == 429


async def test_knockout_mismatch_goes_to_a_person(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    await api.apply(world, job["slug"], cv_pdf("ko"), knockouts={"right_to_work_uae": "no"})
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status, needs_human_review, review_reasons FROM hire_applications",
    )
    assert row.status == "received"  # never rejected by a rule or a model
    assert row.needs_human_review is True
    assert row.review_reasons == ["knockout:right_to_work_uae"]
    missing = await api.start(world, job["slug"], knockouts={})
    assert missing.status_code == 400


async def test_duplicate_applicants_across_jobs(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    backend = await open_job(api, world, BACKEND)
    support = await open_job(api, world, SUPPORT_LEAD)
    shared_cv = cv_pdf("shared")
    await api.apply(world, backend["slug"], shared_cv, ip="198.51.100.1")
    # Same email, another job.
    await api.apply(
        world,
        support["slug"],
        cv_pdf("second"),
        knockouts={"based_in_uae": "yes"},
        ip="198.51.100.2",
    )
    # Another email, same phone.
    await api.apply(
        world,
        backend["slug"],
        cv_pdf("phone"),
        email="l.h@example.test",
        full_name="Someone Else",
        ip="198.51.100.3",
    )
    # Another email and phone, same CV bytes.
    await api.apply(
        world,
        backend["slug"],
        shared_cv,
        email="cv@example.test",
        full_name="Omar Saleh",
        phone="+971 55 000 0000",
        ip="198.51.100.4",
    )
    # Another email and phone, the name reordered with a small spelling difference.
    await api.apply(
        world,
        backend["slug"],
        cv_pdf("name"),
        email="haddad@example.test",
        full_name="Haddad Laila",
        phone=None,
        ip="198.51.100.5",
    )
    # A different person entirely.
    await api.apply(
        world,
        backend["slug"],
        cv_pdf("other"),
        email="mira@example.test",
        full_name="Mira Tan",
        phone="+971 52 999 9999",
        ip="198.51.100.6",
    )
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT duplicate_signals, needs_human_review, review_reasons FROM hire_applications "
        "ORDER BY submitted_at",
    )
    signals = [sorted({s for entry in r.duplicate_signals for s in entry["signals"]}) for r in rows]
    assert signals[0] == []
    assert signals[1] == ["email"]
    assert signals[2] == ["phone"]
    assert signals[3] == ["cv_hash"]
    assert "name" in signals[4]
    assert signals[5] == []
    assert [r.needs_human_review for r in rows] == [False, True, True, True, True, False]
    assert rows[1].review_reasons == ["possible_duplicate"]
    # One candidate row per email address, never merged.
    (count,) = await owner_rows(owner_engine, world.org_id, "SELECT count(*) FROM hire_candidates")
    assert count[0] == 5


# ------------------------------------------------------------------ self-identification report


async def test_self_identification_report_is_for_bias_reporting_only(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    for i in range(6):
        await api.apply(
            world,
            job["slug"],
            cv_pdf(f"sid{i}"),
            email=f"p{i}@example.test",
            full_name=f"Person Number{i}",
            phone=None,
            ip=f"192.0.2.{i + 1}",
            self_id={"gender": "woman" if i < 5 else "man", "disability": "no"},
        )
    for name in ("recruiter", "manager", "interviewer", "employee"):
        denied = await api.call(
            "GET", f"/v1/hire/reports/self-identification?job_id={job['id']}", world, name
        )
        assert denied.status_code == 403, name
    report = await api.call(
        "GET", f"/v1/hire/reports/self-identification?job_id={job['id']}", world, "auditor"
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["responses"] == 6
    assert body["categories"]["gender"]["woman"] == 5
    assert body["categories"]["gender"]["man"] is None  # under 5: suppressed
    assert body["categories"]["disability"]["no"] == 6
    actions = [
        r.action
        for r in await owner_rows(owner_engine, world.org_id, "SELECT action FROM audit_events")
    ]
    assert "hire.selfid.report_viewed" in actions


# ------------------------------------------------------------------ tenant isolation


async def test_tenant_isolation_of_every_hire_table(
    hire_api: HireApi, world: HireWorld, other: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    started = await api.start(world, job["slug"], self_id={"gender": "woman"})
    token = started.json()["token"]
    await api.upload(world, token, cv_pdf("iso"))
    await api.submit(world, token)
    await api.call("PUT", "/v1/hire/settings", world, "admin", json={"response_days": 7})
    (app_item,) = (await api.call("GET", "/v1/hire/applications", world, "recruiter")).json()[
        "items"
    ]

    # Row Level Security: the other organisation's session sees none of these rows.
    async with api.engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(other.org_id)}
        )
        for table in HIRE_TABLES:
            seen = await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert seen == 0, table
    async with api.engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(world.org_id)}
        )
        for table in HIRE_TABLES:
            seen = await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert seen >= 1, table
    async with api.engine.begin() as conn:  # no tenant at all: nothing
        for table in HIRE_TABLES:
            seen = await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert seen == 0, table

    # The API: the other organisation's people get 404s, never the data.
    for path in (
        f"/v1/hire/jobs/{job['id']}",
        f"/v1/hire/jobs/{job['id']}/rubric",
        f"/v1/hire/applications/{app_item['id']}",
    ):
        assert (await api.call("GET", path, other, "admin")).status_code == 404, path
    assert (await api.call("GET", "/v1/hire/jobs", other, "admin")).json()["items"] == []
    assert (await api.call("GET", "/v1/hire/applications", other, "admin")).json()["items"] == []
    # Public routes: a job slug only resolves in its own organisation.
    other_job = await open_job(api, other, SUPPORT_LEAD)
    crossed = await api.client.get(
        f"/v1/hire/public/careers/{other.slug}/jobs/{job['slug']}", headers=api.public()
    )
    assert crossed.status_code == 404
    page = await api.client.get(f"/v1/hire/public/careers/{other.slug}", headers=api.public())
    assert [j["slug"] for j in page.json()["jobs"]] == [other_job["slug"]]
    # An upload token of one organisation does not work under another's slug.
    second = await api.start(world, job["slug"], email="second@example.test")
    stolen = await api.upload(other, second.json()["token"], cv_pdf("stolen"))
    assert stolen.status_code == 404


# ------------------------------------------------------------------ retention


async def test_rejected_candidate_retention_rule(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    assert RULE_KEY in api.app.state.retention_registry
    job = await open_job(api, world)
    await api.apply(world, job["slug"], cv_pdf("old"), email="old@example.test", ip="192.0.2.80")
    await api.apply(world, job["slug"], cv_pdf("new"), email="new@example.test", ip="192.0.2.81")
    # Rejection comes with the pipeline (WP2.5); here the rows are set up directly.
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_applications SET status = 'rejected', rejected_at = now() - "
        "CASE WHEN reference = (SELECT min(reference) FROM hire_applications) "
        "THEN interval '13 months' ELSE interval '2 months' END, "
        "retention_until = now()",
    )
    # Recompute with the default 12 months, through the settings route.
    saved = await api.call(
        "PUT", "/v1/hire/settings", world, "admin", json={"rejected_retention_months": 12}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["rejected_retention_months"] == 12
    no_otp = await api.call(
        "PUT", "/v1/hire/settings", world, "admin", otp=False, json={"response_days": 5}
    )
    assert no_otp.status_code == 403
    assert (
        await api.call("PUT", "/v1/hire/settings", world, "recruiter", json={"response_days": 5})
    ).status_code == 403

    async def due() -> int:
        session = create_sessionmaker(api.engine)()
        async with session:
            set_tenant(session, world.org_id, None)
            async with session.begin():
                counts = await dry_run_org(
                    session, api.app.state.retention_registry, run_id=uuid.uuid4()
                )
        return next(c.would_delete for c in counts if c.rule == RULE_KEY)

    assert await due() == 1
    await api.call(
        "PUT", "/v1/hire/settings", world, "admin", json={"rejected_retention_months": 1}
    )
    assert await due() == 2
    # Dry run only: nothing was deleted.
    (count,) = await owner_rows(
        owner_engine, world.org_id, "SELECT count(*) FROM hire_applications"
    )
    assert count[0] == 2


# ------------------------------------------------------------------ abuse of the public form


async def test_public_form_cannot_change_another_persons_details(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    await api.apply(world, job["slug"], cv_pdf("real"), ip="192.0.2.90")
    # Someone else uses the same address with another name and phone.
    await api.apply(
        world,
        job["slug"],
        cv_pdf("fake"),
        full_name="Mallory Example",
        phone="+971 50 999 0000",
        ip="192.0.2.91",
    )
    session = create_sessionmaker(api.engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            (candidate,) = list(await session.scalars(select(HireCandidate)))
            assert unseal(candidate, "full_name") == "Layla Haddad"
            assert unseal(candidate, "phone") == "+971 50 123 4567"
            apps = list(
                await session.scalars(select(HireApplication).order_by(HireApplication.created_at))
            )
            assert unseal(apps[1], "contact")["full_name"] == "Mallory Example"
            assert "contact_differs" in apps[1].review_reasons
            assert apps[1].needs_human_review is True
    items = (await api.call("GET", "/v1/hire/applications", world, "recruiter")).json()["items"]
    assert sorted(i["candidate_name"] for i in items) == ["Layla Haddad", "Mallory Example"]


async def test_public_limits_on_bodies_and_closed_jobs(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    huge = await api.client.post(
        f"/v1/hire/public/careers/{world.slug}/jobs/{job['slug']}/applications",
        content=b'{"answers": "' + b"x" * 70_000 + b'"}',
        headers=api.public("192.0.2.95") | {"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    started = await api.start(world, job["slug"], ip="192.0.2.96")
    token = started.json()["token"]
    await api.upload(world, token, cv_pdf("closing"), ip="192.0.2.96")
    closed = await api.call("POST", f"/v1/hire/jobs/{job['id']}/close", world, "recruiter")
    assert closed.status_code == 200
    late = await api.submit(world, token, ip="192.0.2.96")
    assert late.status_code == 404
    # Employees see open jobs only, never drafts or closed ones.
    listed = await api.call("GET", "/v1/hire/jobs", world, "employee")
    assert listed.json()["items"] == []
    gone = await api.call("GET", f"/v1/hire/jobs/{job['id']}", world, "employee")
    assert gone.status_code == 404
    seen = await api.call("GET", f"/v1/hire/jobs/{job['id']}", world, "interviewer")
    assert seen.status_code == 200

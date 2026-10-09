# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire jobs, AI drafts, rubric builder and forms on Postgres (WP2.2), offline.

The model router runs in ``recorded`` mode with the shipped recordings
(``pairly_hire/data/recordings.json``); its providers are fakes with no script, so a
live call would fail.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_hire.demo import BACKEND, SUPPORT_LEAD

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

__all__ = ["hire_api"]  # fixture


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


async def audit_actions(owner: AsyncEngine, org_id: uuid.UUID) -> list[str]:
    rows = await owner_rows(owner, org_id, "SELECT action FROM audit_events ORDER BY seq")
    return [r.action for r in rows]


JOB = {
    "title": "Data Analyst",
    "location": "Dubai",
    "work_model": "hybrid",
    "employment_type": "full_time",
}


# ------------------------------------------------------------------ jobs


async def test_job_crud_with_permissions_and_audit(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    for name in ("employee", "interviewer", "auditor"):
        refused = await api.call("POST", "/v1/hire/jobs", world, name, json=JOB)
        assert refused.status_code == 403, name

    created = await api.call(
        "POST",
        "/v1/hire/jobs",
        world,
        "manager",
        otp=False,
        json=JOB | {"salary_min": "9000", "salary_max": "12000", "salary_currency": "AED"},
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["status"] == "draft"
    assert job["slug"] == "data-analyst"
    assert job["show_salary"] is False

    # Pay transparency: showing the salary needs a full range.
    bad = await api.call(
        "PATCH", f"/v1/hire/jobs/{job['id']}", world, "recruiter", json={"show_salary": True}
    )
    assert bad.status_code == 400
    shown = await api.call(
        "PATCH",
        f"/v1/hire/jobs/{job['id']}",
        world,
        "recruiter",
        json={"show_salary": True, "salary_period": "month", "description_ar": "وصف"},
    )
    assert shown.status_code == 200, shown.text
    assert shown.json()["show_salary"] is True
    inverted = await api.call(
        "PATCH", f"/v1/hire/jobs/{job['id']}", world, "recruiter", json={"salary_min": "20000"}
    )
    assert inverted.status_code == 400

    # The hiring team sees drafts; employees only ever see open jobs.
    for name in ("interviewer", "auditor", "admin"):
        listed = await api.call("GET", "/v1/hire/jobs", world, name)
        assert listed.status_code == 200, name
        assert [j["id"] for j in listed.json()["items"]] == [job["id"]]
    hidden = await api.call("GET", "/v1/hire/jobs", world, "employee")
    assert hidden.json()["items"] == []

    second = await api.call("POST", "/v1/hire/jobs", world, "recruiter", json=JOB)
    assert second.json()["slug"].startswith("data-analyst-")

    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("hire.job.created") == 2
    assert actions.count("hire.job.updated") == 1


async def test_publish_needs_a_locked_rubric_and_a_second_factor(
    hire_api: HireApi, world: HireWorld
) -> None:
    api = hire_api
    created = await api.call(
        "POST", "/v1/hire/jobs", world, "recruiter", json=JOB | {"description_en": "About it."}
    )
    job_id = created.json()["id"]
    early = await api.call("POST", f"/v1/hire/jobs/{job_id}/publish", world, "recruiter")
    assert early.status_code == 409
    assert "lock the rubric" in early.json()["detail"]

    rubric = BACKEND.rubric.model_dump(mode="json")
    saved = await api.call("PUT", f"/v1/hire/jobs/{job_id}/rubric", world, "manager", json=rubric)
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_source"] == "person"

    no_otp = await api.call(
        "POST", f"/v1/hire/jobs/{job_id}/rubric/lock", world, "manager", otp=False
    )
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"
    for name in ("interviewer", "admin", "employee"):
        denied = await api.call("POST", f"/v1/hire/jobs/{job_id}/rubric/lock", world, name)
        assert denied.status_code == 403, name
    locked = await api.call("POST", f"/v1/hire/jobs/{job_id}/rubric/lock", world, "manager")
    assert locked.status_code == 200, locked.text
    assert locked.json()["version"]["version"] == 1

    # A hiring manager cannot publish; a recruiter needs the second factor.
    assert (
        await api.call("POST", f"/v1/hire/jobs/{job_id}/publish", world, "manager")
    ).status_code == 403
    no_otp = await api.call(
        "POST", f"/v1/hire/jobs/{job_id}/publish", world, "recruiter", otp=False
    )
    assert no_otp.json()["code"] == "second_factor_required"
    published = await api.call("POST", f"/v1/hire/jobs/{job_id}/publish", world, "recruiter")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "open"
    assert body["form_policy_id"] is not None  # the default form, published on first publish
    paused = await api.call("POST", f"/v1/hire/jobs/{job_id}/pause", world, "recruiter")
    assert paused.json()["status"] == "paused"
    closed = await api.call("POST", f"/v1/hire/jobs/{job_id}/close", world, "recruiter")
    assert closed.json()["status"] == "closed"
    again = await api.call("POST", f"/v1/hire/jobs/{job_id}/publish", world, "recruiter")
    assert again.status_code == 409
    edit = await api.call(
        "PATCH", f"/v1/hire/jobs/{job_id}", world, "recruiter", json={"team": "x"}
    )
    assert edit.status_code == 409


async def test_invalid_rubric_is_refused(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job_id = (await api.call("POST", "/v1/hire/jobs", world, "recruiter", json=JOB)).json()["id"]
    bad = BACKEND.rubric.model_dump(mode="json") | {"must_haves": []}
    refused = await api.call("PUT", f"/v1/hire/jobs/{job_id}/rubric", world, "recruiter", json=bad)
    assert refused.status_code == 400
    assert "must_haves" in refused.json()["detail"]
    nothing = await api.call("POST", f"/v1/hire/jobs/{job_id}/rubric/lock", world, "recruiter")
    assert nothing.status_code == 409


# ------------------------------------------------------------------ AI drafts (recorded)


async def test_job_description_draft_is_recorded_and_checked(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    drafted = await api.call(
        "POST",
        "/v1/hire/jobs/draft-description",
        world,
        "recruiter",
        otp=False,
        json={"brief": BACKEND.brief},
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["title"] == BACKEND.draft.title
    assert body["description_ar"] == BACKEND.draft.description_ar
    assert body["model"]["from_recording"] is True
    assert body["model"]["prompt_version"] == "hire_jd_draft/v1"
    assert body["checks"] == {
        "inclusive_en": [],
        "inclusive_ar": [],
        "sanity_en": [],
        "sanity_ar": [],
    }
    calls = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT task, prompt_version, status FROM model_calls WHERE mode <> 'import'",
    )
    assert [(c.task, c.prompt_version) for c in calls] == [("hire_jd_draft", "v1")]

    # A brief nobody recorded: fail closed with a plain message, nothing made up.
    unknown = await api.call(
        "POST",
        "/v1/hire/jobs/draft-description",
        world,
        "recruiter",
        json={"brief": "A brand new role nobody recorded an answer for, in Sharjah."},
    )
    assert unknown.status_code == 503
    assert unknown.json()["code"] == "model_unavailable"
    assert "yourself" in unknown.json()["detail"]
    employee = await api.call(
        "POST", "/v1/hire/jobs/draft-description", world, "employee", json={"brief": BACKEND.brief}
    )
    assert employee.status_code == 403


async def test_text_checks(hire_api: HireApi, world: HireWorld) -> None:
    checked = await hire_api.call(
        "POST",
        "/v1/hire/jobs/check-text",
        world,
        "interviewer",
        json={
            "title": "Junior Platform Engineer",
            "description_en": "Young rockstar with 12 years of LangChain.",
            "title_ar": "مطلوب مهندس",
            "description_ar": "للذكور فقط",
        },
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert {"en.age.young", "en.jargon.rockstar"} <= {f["rule"] for f in body["inclusive_en"]}
    assert {"ar.gendered.title", "ar.gendered.only"} <= {f["rule"] for f in body["inclusive_ar"]}
    assert {"sanity.years_exceed_age", "sanity.seniority_conflict"} <= {
        f["rule"] for f in body["sanity_en"]
    }


async def test_rubric_draft_lock_and_immutable_versions(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world, SUPPORT_LEAD)
    rubric = (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/rubric", world, "interviewer")
    ).json()
    assert rubric["draft_source"] == "ai"
    assert rubric["locked"] == SUPPORT_LEAD.rubric.model_dump(mode="json")
    assert rubric["draft_differs"] is False
    assert [v["version"] for v in rubric["versions"]] == [1]

    # Locking an unchanged draft again creates nothing.
    again = await api.call("POST", f"/v1/hire/jobs/{job['id']}/rubric/lock", world, "recruiter")
    assert again.json()["new_version"] is False

    # One application on version 1, then the rubric changes.
    await api.apply(world, job["slug"], cv_pdf("lock-test"), knockouts={"based_in_uae": "yes"})
    edited = SUPPORT_LEAD.rubric.model_dump(mode="json")
    edited["must_haves"][0]["weight"] = 10
    put = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/rubric", world, "recruiter", json=edited
    )
    assert put.json()["draft_differs"] is True
    relocked = await api.call("POST", f"/v1/hire/jobs/{job['id']}/rubric/lock", world, "recruiter")
    assert relocked.status_code == 200, relocked.text
    assert relocked.json()["new_version"] is True
    assert relocked.json()["version"]["version"] == 2
    assert relocked.json()["applications_on_earlier_versions"] == 1

    # The application keeps version 1: never rescored silently.
    (application,) = (await api.call("GET", "/v1/hire/applications", world, "recruiter")).json()[
        "items"
    ]
    detail = await api.call("GET", f"/v1/hire/applications/{application['id']}", world, "recruiter")
    assert detail.json()["rubric_version"] == 1

    # Policy versions are immutable for every role, the owner included.
    for statement in (
        "UPDATE policy_versions SET content = 'x' WHERE kind = 'hire_rubric'",
        "DELETE FROM policy_versions WHERE kind = 'hire_rubric'",
    ):
        with pytest.raises(DBAPIError):
            await owner_rows(owner_engine, world.org_id, statement)
    with pytest.raises(DBAPIError):  # noqa: PT012  (the tenant setting, then the write)
        async with api.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(world.org_id)}
            )
            await conn.execute(text("UPDATE policy_versions SET content = 'x'"))
    audit = await audit_actions(owner_engine, world.org_id)
    assert audit.count("hire.rubric.locked") == 3
    assert "core.policy.version_published" in audit


async def test_application_form_builder(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    form = {
        "standard": {"phone": "required", "location": "hidden", "profile_url": "optional"},
        "questions": [
            {"id": "notice", "kind": "text", "label": "Notice period", "required": True},
            {
                "id": "level",
                "kind": "choice",
                "label": "Level",
                "label_ar": "المستوى",
                "options": [{"id": "mid", "label": "Mid"}, {"id": "senior", "label": "Senior"}],
            },
            {"id": "relocate", "kind": "yes_no", "label": "Can you relocate?"},
        ],
        "files": [{"id": "portfolio", "label": "Portfolio"}],
        "self_identification": {"enabled": True, "categories": ["gender"]},
    }
    denied = await api.call(
        "PUT", f"/v1/hire/jobs/{job['id']}/form", world, "interviewer", json=form
    )
    assert denied.status_code == 403
    saved = await api.call("PUT", f"/v1/hire/jobs/{job['id']}/form", world, "recruiter", json=form)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert [k["id"] for k in saved.json()["knockout_questions"]] == ["right_to_work_uae"]
    bad = form | {"questions": [{"id": "x", "kind": "choice", "label": "Pick"}]}
    refused = await api.call("PUT", f"/v1/hire/jobs/{job['id']}/form", world, "recruiter", json=bad)
    assert refused.status_code == 400

    public = await api.client.get(
        f"/v1/hire/public/careers/{world.slug}/jobs/{job['slug']}", headers=api.public()
    )
    public_form = public.json()["form"]
    assert [q["id"] for q in public_form["questions"]] == ["notice", "level", "relocate"]
    assert [f["id"] for f in public_form["files"]] == ["cv", "portfolio"]
    assert [c["id"] for c in public_form["self_identification"]] == ["gender"]
    # Knockout questions are public, the answer the job needs is not.
    assert public_form["knockout_questions"][0].keys() == {"id", "question", "question_ar"}

    missing = await api.start(world, job["slug"], answers={"level": "mid"})
    assert missing.status_code == 400
    assert "notice" in missing.json()["detail"]
    no_phone = await api.start(world, job["slug"], phone=None, answers={"notice": "1 month"})
    assert no_phone.status_code == 400
    ok = await api.start(world, job["slug"], answers={"notice": "1 month", "level": "senior"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["file_fields"] == ["cv", "portfolio"]
    token = ok.json()["token"]
    other = await api.upload(world, token, cv_pdf("portfolio"), field="portfolio")
    assert other.status_code == 201, other.text
    unknown = await api.upload(world, token, cv_pdf("x"), field="photo")
    assert unknown.status_code == 400
    early = await api.submit(world, token)
    assert early.status_code == 400
    assert early.json()["code"] == "cv_required"


# ------------------------------------------------------------------ demo seed


async def test_hire_demo_seed_is_idempotent_and_opens_the_careers_page(
    hire_api: HireApi, owner_engine: AsyncEngine
) -> None:
    from pairly_core.db import create_sessionmaker, set_tenant
    from pairly_core.plugins import SeedContext
    from pairly_core.rbac import build_registry
    from pairly_hire.seed import seed_hire_demo

    from .hire_support import PLUGINS

    made = await seed_hire_world(owner_engine, recordings=False)
    registry = build_registry(PLUGINS)

    async def run() -> list[str]:
        session = create_sessionmaker(owner_engine)()
        async with session:
            set_tenant(session, made.org_id, None)
            async with session.begin():
                result = await seed_hire_demo(
                    session, SeedContext(made.org_id, made.slug, registry, "https://app.test")
                )
        return list(result.lines)

    first = await run()
    assert any("open, rubric version 1 locked by the seed" in line for line in first)
    assert any("draft with an unlocked AI rubric draft" in line for line in first)
    assert any("6 imported" in line for line in first)
    second = await run()
    assert all("open, rubric" not in line for line in second)
    assert any("6 already there" in line for line in second)

    page = await hire_api.client.get(
        f"/v1/hire/public/careers/{made.slug}", headers=hire_api.public()
    )
    assert sorted(j["slug"] for j in page.json()["jobs"]) == [
        "customer-support-lead",
        "senior-backend-engineer",
    ]
    # The seeded AP job can be finished by a person: its AI draft locks offline.
    jobs = (await hire_api.call("GET", "/v1/hire/jobs?status=draft", made, "recruiter")).json()
    (draft,) = jobs["items"]
    redrafted = await hire_api.call(
        "POST", f"/v1/hire/jobs/{draft['id']}/rubric/draft", made, "recruiter"
    )
    assert redrafted.status_code == 200, redrafted.text
    locked = await hire_api.call(
        "POST", f"/v1/hire/jobs/{draft['id']}/rubric/lock", made, "manager"
    )
    assert locked.status_code == 200, locked.text
    await retire_documents(owner_engine, made.org_id)

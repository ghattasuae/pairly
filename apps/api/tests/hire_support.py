# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Support for the Pairly Hire database tests (WP2.2 jobs and rubrics, WP2.3 apply flow).

* ``HireWorld``: an organisation with one member per Hire role, its recorded model
  responses imported, and helpers to sign in as each of them.
* ``hire_api``: the API with in-memory document storage, a recording job queue, the
  model router in ``recorded`` mode (scripted fake providers without a script, so a live
  call would fail) and the fake Turnstile verifier of the test environment.
* ``open_job``: a job made through the API the way a recruiter does it (draft, AI
  rubric draft, lock with a second factor, publish).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.settings import ModelMode, load_llm_settings
from pairly_core.plugins import WorkflowPlugin, discover_plugins
from pairly_core.workflow.services import GraphServices
from pairly_hire.antispam import DUMMY_TOKEN
from pairly_hire.demo import BACKEND, DemoJob
from pairly_hire.seed import recordings_text

from .conftest import DbUrls, TokenFactory, make_settings
from .notify_support import notify_settings

PLUGINS: list[WorkflowPlugin] = list(discover_plugins().values())

# name -> memberships (role, workflow).
PEOPLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "recruiter": (("recruiter", "hire"),),
    "manager": (("hiring_manager", "hire"),),
    "interviewer": (("interviewer", "hire"),),
    "employee": (("employee", None),),
    "admin": (("admin", None),),
    "auditor": (("auditor", None),),
}


@dataclasses.dataclass
class HireWorld:
    org_id: uuid.UUID
    slug: str
    name: str
    people: dict[str, tuple[uuid.UUID, str]] = dataclasses.field(default_factory=dict)


async def seed_hire_world(owner: AsyncEngine, *, recordings: bool = True) -> HireWorld:
    org_id = uuid.uuid4()
    world = HireWorld(org_id=org_id, slug=f"hire-{org_id.hex[:12]}", name="Acme Logistics")
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": org_id, "n": world.name, "s": world.slug},
        )
        for name, memberships in PEOPLE.items():
            user_id = uuid.uuid4()
            subject = f"sub-hire-{name}-{user_id.hex[:10]}"
            await conn.execute(
                text("SELECT set_config('app.auth_subject', :s, true)"), {"s": subject}
            )
            await conn.execute(
                text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
                {"id": user_id, "s": subject, "e": f"{name}-{user_id.hex[:6]}@example.test"},
            )
            for role, workflow in memberships:
                await conn.execute(
                    text(
                        "INSERT INTO memberships (org_id, user_id, role_id, workflow) "
                        "SELECT :o, :u, r.id, :w FROM roles r "
                        "WHERE r.key = :role AND r.org_id IS NULL"
                    ),
                    {"o": org_id, "u": user_id, "w": workflow, "role": role},
                )
            world.people[name] = (user_id, subject)
    if recordings:
        session = create_sessionmaker(owner)()
        async with session:
            set_tenant(session, org_id, None)
            async with session.begin():
                await import_recordings(session, org_id, parse_bundle(recordings_text()))
    return world


async def retire_documents(owner: AsyncEngine, org_id: uuid.UUID) -> None:
    """Park this organisation's CVs, so the stalled-document sweeper other tests run
    over every organisation never picks them up (nothing scans them here)."""
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(
            text(
                "UPDATE documents SET status = 'parse_failed', updated_at = now() "
                "WHERE org_id = :o AND status IN ('received', 'scanning', 'clean')"
            ),
            {"o": org_id},
        )


def ingestion_settings() -> IngestionSettings:
    return IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
    )


@dataclasses.dataclass
class HireApi:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    engine: AsyncEngine
    store: InMemoryObjectStore
    queue: RecordingJobQueue

    def headers(
        self, world: HireWorld, name: str, *, otp: bool = True, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name][1], **claims)
        return {"Authorization": f"Bearer {token}", "X-Pairly-Org": str(world.org_id)} | (
            extra or {}
        )

    async def call(
        self,
        method: str,
        path: str,
        world: HireWorld,
        name: str,
        *,
        otp: bool = True,
        json: Any = None,
    ) -> httpx.Response:
        return await self.client.request(
            method, path, json=json, headers=self.headers(world, name, otp=otp)
        )

    # ------------------------------------------------------------ public flow

    def public(self, ip: str = "198.51.100.7") -> dict[str, str]:
        return {"X-Pairly-Client-IP": ip}

    async def start(
        self,
        world: HireWorld,
        job_slug: str,
        *,
        email: str = "layla.haddad@example.test",
        full_name: str = "Layla Haddad",
        phone: str | None = "+971 50 123 4567",
        knockouts: dict[str, str] | None = None,
        answers: dict[str, Any] | None = None,
        self_id: dict[str, str] | None = None,
        turnstile: str = DUMMY_TOKEN,
        consent: bool = True,
        notice_version: str | None = None,
        locale: str = "en",
        ip: str = "198.51.100.7",
    ) -> httpx.Response:
        from pairly_hire.notice import CURRENT_VERSION

        body = {
            "consent": {"notice_version": notice_version or CURRENT_VERSION, "accepted": consent},
            "contact": {
                "full_name": full_name,
                "email": email,
                "phone": phone,
                "location": "Dubai",
                "profile_url": "https://profiles.example.test/layla",
                "locale": locale,
            },
            "answers": answers or {},
            "knockouts": knockouts if knockouts is not None else {"right_to_work_uae": "yes"},
            "self_identification": self_id or {},
            "turnstile_token": turnstile,
        }
        return await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/jobs/{job_slug}/applications",
            json=body,
            headers=self.public(ip),
        )

    async def upload(
        self,
        world: HireWorld,
        token: str,
        data: bytes,
        *,
        field: str = "cv",
        filename: str = "cv.pdf",
        ip: str = "198.51.100.7",
    ) -> httpx.Response:
        return await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/applications/files",
            files={"file": (filename, data, "application/pdf")},
            data={"token": token, "field": field},
            headers=self.public(ip),
        )

    async def submit(
        self, world: HireWorld, token: str, *, ip: str = "198.51.100.7"
    ) -> httpx.Response:
        return await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/applications/submit",
            json={"token": token},
            headers=self.public(ip),
        )

    async def apply(self, world: HireWorld, job_slug: str, cv: bytes, **kwargs: Any) -> str:
        """The whole flow; returns the reference."""
        ip = kwargs.get("ip", "198.51.100.7")
        started = await self.start(world, job_slug, **kwargs)
        assert started.status_code == 201, started.text
        token = started.json()["token"]
        uploaded = await self.upload(world, token, cv, ip=ip)
        assert uploaded.status_code == 201, uploaded.text
        submitted = await self.submit(world, token, ip=ip)
        assert submitted.status_code == 202, submitted.text
        return str(submitted.json()["reference"])


def cv_pdf(label: str) -> bytes:
    """A tiny synthetic PDF; ``label`` makes its bytes (and SHA-256) distinct."""
    return b"%PDF-1.4\n% synthetic CV " + label.encode() + b"\n%%EOF\n"


@pytest.fixture
async def hire_api(
    migrated: DbUrls, tokens: TokenFactory, tmp_path: Path
) -> AsyncIterator[HireApi]:
    store = InMemoryObjectStore()
    queue = RecordingJobQueue()
    documents = DocumentServices(
        settings=ingestion_settings(), store=store, queue=queue, tmp_dir=tmp_path
    )
    engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    router = build_router(
        load_llm_settings(model_mode=ModelMode.RECORDED),
        create_sessionmaker(engine),
        providers={"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")},
    )
    app = create_app(
        make_settings(migrated.app),
        plugins=PLUGINS,
        documents=documents,
        notification_settings=notify_settings(),
        slack_transport=FakeSlackTransport(),
        graph_services=GraphServices(store=store, router=router),
    )
    try:
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield HireApi(app, c, tokens, engine, store, queue)
    finally:
        await engine.dispose()


async def open_job(api: HireApi, world: HireWorld, demo: DemoJob = BACKEND) -> dict[str, Any]:
    """A job created, rubric drafted by the AI (recorded), locked and published."""
    created = await api.call(
        "POST",
        "/v1/hire/jobs",
        world,
        "recruiter",
        json={
            "title": demo.draft.title,
            "title_ar": demo.draft.title_ar,
            "team": demo.team,
            "location": demo.location,
            "country": demo.country,
            "work_model": demo.work_model,
            "employment_type": demo.employment_type,
            "salary_min": str(demo.salary_min),
            "salary_max": str(demo.salary_max),
            "salary_currency": demo.salary_currency,
            "salary_period": demo.salary_period,
            "show_salary": demo.show_salary,
            "description_en": demo.draft.description_en,
            "description_ar": demo.draft.description_ar,
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()
    drafted = await api.call("POST", f"/v1/hire/jobs/{job['id']}/rubric/draft", world, "recruiter")
    assert drafted.status_code == 200, drafted.text
    locked = await api.call("POST", f"/v1/hire/jobs/{job['id']}/rubric/lock", world, "recruiter")
    assert locked.status_code == 200, locked.text
    published = await api.call("POST", f"/v1/hire/jobs/{job['id']}/publish", world, "recruiter")
    assert published.status_code == 200, published.text
    return dict(published.json())


async def owner_rows(
    owner: AsyncEngine, org_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""WP2.4: the ``hire_screening`` graph end to end, on real Postgres, offline.

The model router runs in ``recorded`` mode with the scripted screening bundle
(``synth/hire/recordings/hire_screening.json``: classifier, CV parse and scoring answers
scripted from the synthetic ground truth, not model output); its providers are fakes
without a script, so any live call fails. CVs are the synthetic demo applications
(``synth.hire.screening.DEMO_APPLICATIONS``), uploaded through the public apply flow;
the scan uses the fake scanner and parsing runs in the real sandbox.

1. Happy path: parse, guardrails, sanitise, CV profile, blind redaction, scoring with
   evidence, knockouts and claim checks, then a person decides. Blind until shortlisted.
   Decision records for extraction, score and suggestion; replays match.
2. Attacks: hidden text and a paraphrased instruction never earn credit; a claim attack
   is flagged, never penalised; a knockout mismatch goes to a person, never rejected.
3. Fail closed: no recording (every model step down), a CV that does not parse, an
   infected file, no model router, and a tampered sanitised or blinded input: every
   one ends in the person's queue with the reason.
4. Reviewer API: permissions, overrides with a reason (decision record, replay),
   re-runs, settings, tenant isolation.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.audit import ActorType
from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.documents.pipeline import ParseConfig
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.scan import EICAR, FakeScanner
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.jobs.registry import JobContext
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.settings import ModelMode, load_llm_settings
from pairly_core.rbac import build_registry
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_core.workflow.services import GraphServices
from pairly_hire.antispam import DUMMY_TOKEN
from pairly_hire.messaging import library
from pairly_hire.notice import CURRENT_VERSION
from pairly_hire.scoring.aggregate import overall_score
from pairly_hire.screening import graph as screening_graph
from pairly_hire.screening.blind import PLACEHOLDER
from pairly_hire.screening.spec import HIRE_SCREENING
from pairly_hire.screening.start import start_pending
from synth.hire.dataset import DocSpec, build
from synth.hire.screening import (
    contact_for,
    job_by_id,
    knockout_answers,
    recordings_text,
    screening_rubric,
    upload_name,
)

from .conftest import DbUrls, TokenFactory, make_settings
from .hire_support import PLUGINS, HireWorld, owner_rows, seed_hire_world
from .notify_support import approval_hooks, notify_settings

JOB = job_by_id("backend_engineer")
RUBRIC = screening_rubric(JOB)
_SPECS: dict[str, DocSpec] = {}


def spec(doc_id: str) -> DocSpec:
    if not _SPECS:
        _SPECS.update({s.record.id: s for s in build(300, 2026)})
    return _SPECS[doc_id]


def label(doc_id: str) -> Any:
    return next(lab for lab in spec(doc_id).record.labels if lab.job_id == JOB.id)


@dataclasses.dataclass
class Screen:
    app: Any
    client: httpx.AsyncClient
    tokens: TokenFactory
    engine: AsyncEngine
    services: GraphServices

    def headers(self, world: HireWorld, name: str, *, otp: bool = True) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name][1], **claims)
        return {
            "Authorization": f"Bearer {token}",
            "X-Pairly-Org": str(world.org_id),
            "Idempotency-Key": f"k-{uuid.uuid4().hex}",
        }

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

    async def open_job(self, world: HireWorld) -> dict[str, Any]:
        created = await self.call(
            "POST",
            "/v1/hire/jobs",
            world,
            "recruiter",
            json={
                "title": JOB.title,
                "location": "Dubai, United Arab Emirates",
                "country": "AE",
                "work_model": "hybrid",
                "employment_type": "full_time",
                "description_en": JOB.summary,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        put = await self.call(
            "PUT",
            f"/v1/hire/jobs/{job['id']}/rubric",
            world,
            "recruiter",
            json=RUBRIC.model_dump(mode="json"),
        )
        assert put.status_code == 200, put.text
        locked = await self.call(
            "POST", f"/v1/hire/jobs/{job['id']}/rubric/lock", world, "recruiter"
        )
        assert locked.status_code == 200, locked.text
        published = await self.call(
            "POST", f"/v1/hire/jobs/{job['id']}/publish", world, "recruiter"
        )
        assert published.status_code == 200, published.text
        return dict(published.json())

    async def apply(
        self,
        world: HireWorld,
        job: dict[str, Any],
        doc_id: str,
        *,
        data: bytes | None = None,
        knockouts: dict[str, str] | None = None,
    ) -> str:
        """The public apply flow with a synthetic CV. Returns the application id."""
        s = spec(doc_id)
        contact = contact_for(s)
        public = {"X-Pairly-Client-IP": f"198.51.100.{uuid.uuid4().int % 200 + 1}"}
        started = await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/jobs/{job['slug']}/applications",
            json={
                "consent": {"notice_version": CURRENT_VERSION, "accepted": True},
                "contact": {
                    "full_name": contact["full_name"],
                    "email": contact["email"],
                    "phone": contact["phone"],
                    "location": contact["location"],
                    "profile_url": contact["profile_url"],
                    "locale": "en",
                },
                "answers": {},
                "knockouts": knockouts if knockouts is not None else knockout_answers(s, JOB.id),
                "self_identification": {},
                "turnstile_token": DUMMY_TOKEN,
            },
            headers=public,
        )
        assert started.status_code == 201, started.text
        token = started.json()["token"]
        name = upload_name(s)
        media = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if name.endswith(".docx")
            else "application/pdf"
        )
        uploaded = await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/applications/files",
            files={"file": (name, data if data is not None else s.render(), media)},
            data={"token": token, "field": "cv"},
            headers=public,
        )
        assert uploaded.status_code == 201, uploaded.text
        submitted = await self.client.post(
            f"/v1/hire/public/careers/{world.slug}/applications/submit",
            json={"token": token},
            headers=public,
        )
        assert submitted.status_code == 202, submitted.text
        reference = submitted.json()["reference"]
        listed = await self.call(
            "GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter"
        )
        (found,) = [a for a in listed.json()["items"] if a["reference"] == reference]
        return str(found["id"])

    async def screening(
        self, world: HireWorld, app_id: str, name: str = "recruiter"
    ) -> dict[str, Any]:
        response = await self.call("GET", f"/v1/hire/applications/{app_id}/screening", world, name)
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def decide(self, world: HireWorld, approval_id: str, decision: str) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/approvals/{approval_id}/decision",
            world,
            "recruiter",
            json={"decision": decision, "reason": "Checked the evidence"},
        )


def parse_config() -> ParseConfig:
    return ParseConfig(
        options=ParseOptions(ocr_languages="eng"),
        limits=SandboxLimits(wall_seconds=60, memory_bytes=768 * 1024 * 1024),
    )


ScreenFactory = Callable[..., Any]


@pytest.fixture
async def screen_factory(
    migrated: DbUrls, tokens: TokenFactory, tmp_path: Path
) -> AsyncIterator[ScreenFactory]:
    stack: list[Any] = []

    async def make(*, router: bool = True) -> Screen:
        store = InMemoryObjectStore()
        documents = DocumentServices(
            settings=_ingestion(), store=store, queue=RecordingJobQueue(), tmp_dir=tmp_path
        )
        engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
        model_router = build_router(
            load_llm_settings(model_mode=ModelMode.RECORDED),
            create_sessionmaker(engine),
            providers={"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")},
        )
        services = GraphServices(
            store=store,
            router=model_router if router else None,
            scanner=FakeScanner(),
            parse_config=parse_config(),
        )
        settings = notify_settings()
        app = create_app(
            make_settings(migrated.app),
            plugins=PLUGINS,
            documents=documents,
            approval_hooks=approval_hooks(settings),
            notification_settings=settings,
            slack_transport=FakeSlackTransport(),
            graph_services=services,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        )
        stack.append((client, lifespan, engine))
        return Screen(app, client, tokens, engine, services)

    yield make
    for client, lifespan, engine in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)
        await engine.dispose()


def _ingestion() -> IngestionSettings:
    return IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
        parse_memory_mb=768,
        ocr_languages="eng",
    )


async def screening_world(owner: AsyncEngine, *, recordings: bool = True) -> HireWorld:
    world = await seed_hire_world(owner)
    if recordings:
        session = create_sessionmaker(owner)()
        async with session:
            set_tenant(session, world.org_id, None)
            async with session.begin():
                await import_recordings(session, world.org_id, parse_bundle(recordings_text()))
    return world


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await screening_world(owner_engine)


async def record_kinds(
    owner: AsyncEngine, world: HireWorld, ids: list[str]
) -> dict[str, list[str]]:
    rows = await owner_rows(
        owner,
        world.org_id,
        "SELECT id, kind FROM decision_records WHERE id = ANY(:ids)",
        {"ids": ids},
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r.kind, []).append(r.id)
    return out


def _quote_shown(text: str, evidence: dict[str, Any]) -> bool:
    shown = text[evidence["display_start"] : evidence["display_end"]]
    return " ".join(shown.split()).casefold() == " ".join(evidence["quote"].split()).casefold()


# ------------------------------------------------------------------ 1. happy path


async def test_screening_end_to_end_blind_until_shortlisted(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    view = await api.screening(world, app_id)
    assert view["status"] == "awaiting_review", view["review_reasons"]
    assert view["run_status"] == "waiting_approval"
    # Blind: no name, contact details or CV file for reviewers.
    name = spec("cv-0026").record.ground_truth.contact.name
    email = spec("cv-0026").record.ground_truth.contact.email
    assert view["blind"] is True
    assert view["document_id"] is None
    assert view["text"]
    assert name not in view["text"]
    assert email not in view["text"]
    assert PLACEHOLDER[next(iter(PLACEHOLDER))] in view["text"]
    assert view["profile"]["contact"] is None
    assert view["profile"]["links"] == []
    assert view["blind_config"]["categories"]["name"] >= 1
    # Scores from the locked rubric version with evidence; the overall score is computed.
    assert view["rubric_version"] == 1
    assert view["scored"] is True
    scores = {c["criterion_id"]: c["score"] for c in view["criteria"]}
    assert scores == {c.criterion_id: c.score for c in label("cv-0026").criteria}
    assert view["overall_score"] == overall_score(RUBRIC, scores) == label("cv-0026").overall
    for criterion in view["criteria"]:
        if criterion["score"] > 0:
            assert criterion["evidence"], criterion
        for evidence in criterion["evidence"]:
            assert _quote_shown(view["text"], evidence)
    assert view["models"]["score"]["task"] == "hire_cv_score"
    assert view["models"]["score"]["prompt_version"] == "v1"
    assert view["models"]["score"]["from_recording"] is True
    assert view["models"]["cv_parse"]["status"] in {"ok", "needs_human"}
    assert view["models"]["classifier"]["ran"] is True
    assert view["knockouts"]
    assert all(k["matches"] for k in view["knockouts"])
    kinds = await record_kinds(owner_engine, world, view["decision_ids"])
    assert {"extraction", "score", "shortlist_suggestion"} <= set(kinds)
    # The score record names the rubric version and the prompt.
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT rubric_version_id, prompt_versions, replay_handler, output FROM decision_records "
        "WHERE id = :id",
        {"id": view["score_decision_id"]},
    )
    assert str(row.rubric_version_id) == view["rubric_policy_id"]
    assert row.prompt_versions[0]["task"] == "hire_cv_score"
    assert row.replay_handler == "hire.score_aggregation"
    assert row.output["overall"] == view["overall_score"]
    # The approval inbox shows no name either.
    approval = await api.call("GET", f"/v1/approvals/{view['approval_id']}", world, "recruiter")
    assert approval.status_code == 200
    context = approval.json()["context"]
    assert context["blind"] is True
    assert "document_id" not in context
    assert name not in str(context)
    assert email not in str(context)
    # Replays of the score and the suggestion match.
    for decision_id in (view["score_decision_id"], kinds["shortlist_suggestion"][0]):
        replayed = await api.call(
            "POST", f"/v1/decisions/{decision_id}/replay", world, "admin", json={"mode": "recorded"}
        )
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["status"] == "match", replayed.json()
    # A person shortlists: blind lifts, the final action ran through the executor.
    decided = await api.decide(world, view["approval_id"], "approve")
    assert decided.status_code == 200, decided.text
    done = await api.screening(world, app_id)
    assert done["status"] == "shortlisted"
    assert done["blind"] is False
    assert name in done["text"]
    assert done["document_id"] is not None
    kinds = await record_kinds(owner_engine, world, done["decision_ids"])
    assert {"approval", "final_action"} <= set(kinds)


async def test_blind_application_hides_identity_on_pipeline_and_messaging_routes(
    screen_factory: ScreenFactory, world: HireWorld
) -> None:
    """Blind mode reaches every pipeline and messaging route (``pipeline.hooks`` wired to
    ``screening.service.is_blind``) and lifts at the shortlist."""
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    view = await api.screening(world, app_id)
    assert view["status"] == "awaiting_review"
    assert view["blind"] is True
    contact = contact_for(spec("cv-0026"))
    secrets = [contact["full_name"], contact["email"], contact["phone"]]

    async def get(path: str) -> dict[str, Any]:
        response = await api.call("GET", path, world, "recruiter")
        assert response.status_code == 200, (path, response.text)
        return dict(response.json())

    drafted = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/messages",
        world,
        "recruiter",
        json={"template_key": library.STATUS_UPDATE},
    )
    assert drafted.status_code == 201, drafted.text
    message_id = drafted.json()["id"]

    async def routes() -> dict[str, dict[str, Any]]:
        return {
            "board": await get(f"/v1/hire/jobs/{job['id']}/pipeline"),
            "profile": await get(f"/v1/hire/pipeline/applications/{app_id}"),
            "list": await get(f"/v1/hire/applications?job_id={job['id']}"),
            "detail": await get(f"/v1/hire/applications/{app_id}"),
            "waiting": await get(f"/v1/hire/pipeline/waiting?job_id={job['id']}"),
            "message": await get(f"/v1/hire/messages/{message_id}"),
        }

    def check_hidden(seen: dict[str, dict[str, Any]]) -> None:
        for name, body in seen.items():
            dumped = json.dumps(body, ensure_ascii=False)
            for value in secrets:
                assert value not in dumped, (name, value)
        (card,) = seen["board"]["cards"]
        assert card["identity_hidden"]
        assert card["label"].startswith("Candidate ")
        assert seen["profile"]["identity_hidden"]
        assert seen["profile"]["contact"] is None
        assert seen["profile"]["files"] == []
        assert seen["list"]["items"][0]["identity_hidden"]
        assert seen["detail"]["cv_document_id"] is None
        assert seen["detail"]["files"] == {}
        assert seen["waiting"]["items"][0]["label"].startswith("Candidate ")
        assert seen["message"]["preview"]["masked"]

    blind = await routes()
    check_hidden(blind)
    # The profile shows the screening result (``set_screening_view``), quotes from the
    # blinded text only.
    summary = blind["profile"]["screening"]
    assert summary["status"] == "awaiting_review"
    assert summary["overall"]
    assert summary["criteria"]
    assert {c["result"] for c in summary["criteria"]} <= {"met", "partly_met", "not_met", "unknown"}
    # Turning the organisation's blind mode off later does not unblind a blind screening.
    saved = await api.call(
        "PUT", "/v1/hire/screening/settings", world, "admin", json={"blind_mode": False}
    )
    assert saved.status_code == 200, saved.text
    check_hidden(await routes())

    # The shortlist lifts blind mode on every route.
    decided = await api.decide(world, view["approval_id"], "approve")
    assert decided.status_code == 200, decided.text
    shown = await routes()
    (card,) = shown["board"]["cards"]
    assert not card["identity_hidden"]
    assert card["label"] == contact["full_name"]
    assert shown["profile"]["contact"]["full_name"] == contact["full_name"]
    assert shown["profile"]["contact"]["email"] == contact["email"]
    assert shown["list"]["items"][0]["candidate_name"] == contact["full_name"]
    assert shown["detail"]["email"] == contact["email"]
    assert shown["detail"]["cv_document_id"] is not None
    assert shown["waiting"]["items"][0]["label"] == contact["full_name"]
    assert not shown["message"]["preview"]["masked"]
    assert contact["full_name"] in shown["message"]["preview"]["text"]


async def test_not_shortlisted_and_rerun(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0029")
    view = await api.screening(world, app_id)
    assert view["status"] == "awaiting_review"
    # Nobody can start a second screening while one waits for a person.
    busy = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/screening/run", world, "recruiter"
    )
    assert busy.status_code == 409
    decided = await api.decide(world, view["approval_id"], "request_changes")
    assert decided.status_code == 200, decided.text
    assert (await api.screening(world, app_id))["status"] == "returned"
    # A person asks for a new screening (second factor, people only).
    no_otp = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/screening/run", world, "recruiter", otp=False
    )
    assert no_otp.status_code == 403
    rerun = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/screening/run", world, "recruiter"
    )
    assert rerun.status_code == 202, rerun.text
    again = await api.screening(world, app_id)
    assert again["id"] == rerun.json()["screening_id"]
    assert again["status"] == "awaiting_review"
    decided = await api.decide(world, again["approval_id"], "reject")
    assert decided.status_code == 200
    final = await api.screening(world, app_id)
    assert final["status"] == "not_shortlisted"
    assert final["blind"] is True
    # Not shortlisted is a screening outcome, never an application rejection.
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM hire_applications WHERE id = :i",
        {"i": app_id},
    )
    assert row.status == "received"


# ------------------------------------------------------------------ 2. attacks and checks


async def test_hidden_text_and_paraphrase_earn_nothing(
    screen_factory: ScreenFactory, world: HireWorld
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    hidden = await api.screening(world, await api.apply(world, job, "atk-white-text-skills-pdf"))
    assert hidden["status"] == "awaiting_review"
    assert any(r.startswith("guardrails_flagged") for r in hidden["review_reasons"])
    removed = [f for f in hidden["guardrail_findings"] if f["type"] == "hidden_text"]
    assert removed
    assert all(f["removed_from_model_input"] for f in removed)
    assert all(f["explanation"] for f in hidden["guardrail_findings"])
    payload = spec("atk-white-text-skills-pdf").record.attack
    assert payload is not None
    for criterion in hidden["criteria"]:
        for evidence in criterion["evidence"]:
            assert all(p not in evidence["quote"] for p in payload.payloads if p)
    paraphrase = await api.screening(world, await api.apply(world, job, "atk-paraphrased-en-1-pdf"))
    codes = {f["code"] for f in paraphrase["guardrail_findings"]}
    assert "classifier_paraphrase" in codes
    assert any(r.startswith("guardrails_flagged") for r in paraphrase["review_reasons"])


async def test_claim_flags_are_information_and_knockouts_never_reject(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    inflated = await api.screening(
        world, await api.apply(world, job, "atk-title-inflation-backend-pdf")
    )
    assert "title_inflation" in {f["code"] for f in inflated["flags"]}
    assert all(f["severity"] == "info" for f in inflated["flags"])
    # Flags never change the score: it is the label's, computed from the criteria.
    assert inflated["overall_score"] == label("atk-title-inflation-backend-pdf").overall
    # A knockout answer that differs goes to a person with a suggested action.
    app_id = await api.apply(world, job, "cv-0057")
    view = await api.screening(world, app_id)
    missed = [k for k in view["knockouts"] if not k["matches"]]
    assert missed
    assert all(k["route"] == "human_review" for k in missed)
    assert {k["suggested_action"] for k in missed} <= {"confirm_with_candidate", "ask_candidate"}
    assert any(r.startswith("knockout:") for r in view["review_reasons"])
    assert view["status"] == "awaiting_review"
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM hire_applications WHERE id = :i",
        {"i": app_id},
    )
    assert row.status == "received"


# ------------------------------------------------------------------ 3. fail closed


async def test_every_model_step_down_still_reaches_a_person(
    screen_factory: ScreenFactory, owner_engine: AsyncEngine
) -> None:
    world = await screening_world(owner_engine, recordings=False)
    api = await screen_factory()
    job = await api.open_job(world)
    view = await api.screening(world, await api.apply(world, job, "cv-0026"))
    assert view["status"] == "awaiting_review"
    assert view["run_status"] == "waiting_approval"
    reasons = view["review_reasons"]
    # The classifier could not run: a flag (Hire fails closed on it).
    assert any(f["code"] == "classifier_unavailable" for f in view["guardrail_findings"])
    assert any(r.startswith("guardrails_flagged") for r in reasons)
    assert any(r.startswith("cv_parse:model_unavailable") for r in reasons)
    assert "blind_without_profile" in reasons
    assert any(r.startswith("score_model_unavailable") for r in reasons)
    assert view["scored"] is False
    assert view["overall_score"] is None
    # Still blind: the application form's contact details hide the name.
    name = spec("cv-0026").record.ground_truth.contact.name
    assert view["blind"] is True
    assert name not in (view["text"] or "")


async def test_unparsed_and_infected_cvs_reach_a_person(
    screen_factory: ScreenFactory, world: HireWorld
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    broken = await api.screening(
        world, await api.apply(world, job, "cv-0026", data=b"%PDF-1.4\nnot really a pdf\n%%EOF\n")
    )
    assert broken["status"] == "awaiting_review"
    assert any(r.startswith("document_not_parsed") for r in broken["review_reasons"])
    assert "claims_skipped" in broken["review_reasons"]
    infected = await api.screening(
        world, await api.apply(world, job, "cv-0029", data=b"%PDF-1.4\n" + EICAR + b"\n%%EOF\n")
    )
    assert infected["status"] == "awaiting_review"
    assert any(r.startswith("document_not_parsed") for r in infected["review_reasons"])


async def test_no_model_router_reaches_a_person(
    screen_factory: ScreenFactory, world: HireWorld
) -> None:
    api = await screen_factory(router=False)
    job = await api.open_job(world)
    view = await api.screening(world, await api.apply(world, job, "cv-0026"))
    assert view["status"] == "awaiting_review"
    assert "cv_parse_skipped:model_router_unavailable" in view["review_reasons"]
    assert "score_skipped:model_router_unavailable" in view["review_reasons"]
    assert any(f["code"] == "classifier_unavailable" for f in view["guardrail_findings"])


async def test_tampered_inputs_never_reach_a_model(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    """The nodes recheck the digests of the sanitised and blinded text before any model
    call; a mismatch (a row changed behind their back) goes to the person instead."""
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    view = await api.screening(world, app_id)
    state = {
        "screening_id": view["id"],
        "application_id": app_id,
        "document_id": str(
            (
                await owner_rows(
                    owner_engine,
                    world.org_id,
                    "SELECT document_id FROM hire_screenings WHERE id = :i",
                    {"i": view["id"]},
                )
            )[0].document_id
        ),
    }
    runtime = api.app.state.workflow_runtime
    assert isinstance(runtime, WorkflowRuntime)
    ctx = WorkflowContext(
        org_id=world.org_id,
        run_id=uuid.UUID(str(view["run_id"])),
        graph=HIRE_SCREENING,
        started_by_type=ActorType.AGENT,
        started_by_id="test",
        sessionmaker=runtime.sessionmaker,
        registry=runtime.registry,
        hooks=runtime.hooks,
        services=runtime.services,
    )
    node_runtime: Any = SimpleNamespace(context=ctx)
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_screenings SET blinded_sha256 = :h WHERE id = :i",
        {"h": "0" * 64, "i": view["id"]},
    )
    command = await screening_graph.score(state, node_runtime)  # type: ignore[arg-type]
    assert command.goto == "checks"
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_screenings SET sanitized_sha256 = :h WHERE id = :i",
        {"h": "1" * 64, "i": view["id"]},
    )
    command = await screening_graph.parse_cv(state, node_runtime)  # type: ignore[arg-type]
    assert command.goto == "checks"
    reasons = (await api.screening(world, app_id))["review_reasons"]
    assert "score_skipped:blinded_input_changed" in reasons
    assert "cv_parse_skipped:sanitized_input_changed" in reasons


async def test_worker_job_starts_pending_screenings(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    # Pretend the API never started it: drop the screening, then let the job find it.
    await owner_rows(
        owner_engine,
        world.org_id,
        "DELETE FROM hire_screenings WHERE application_id = :a",
        {"a": app_id},
    )
    runtime = api.app.state.workflow_runtime
    counts = await start_pending(JobContext(runtime.sessionmaker, runtime, runtime.registry))
    assert counts["started"] >= 1
    assert counts["errors"] == 0
    assert (await api.screening(world, app_id))["status"] == "awaiting_review"


async def test_a_screening_whose_run_never_started_can_be_rerun(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    first = await api.screening(world, app_id)
    # As if the run had never started (the process stopped right after creating it).
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_screenings SET status = 'processing', run_id = NULL, "
        "created_at = now() - interval '10 minutes' WHERE id = :i",
        {"i": first["id"]},
    )
    rerun = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/screening/run", world, "recruiter"
    )
    assert rerun.status_code == 202, rerun.text
    (old,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status, failure_reason FROM hire_screenings WHERE id = :i",
        {"i": first["id"]},
    )
    assert (old.status, old.failure_reason) == ("failed", "run_not_started")
    assert (await api.screening(world, app_id))["status"] == "awaiting_review"


# ------------------------------------------------------------------ 4. reviewer API


async def test_reviewer_permissions_overrides_and_isolation(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    path = f"/v1/hire/applications/{app_id}/screening"
    # The hiring team and auditors read it; an employee does not.
    for name in ("recruiter", "manager", "interviewer", "auditor"):
        assert (await api.call("GET", path, world, name)).status_code == 200, name
    assert (await api.call("GET", path, world, "employee")).status_code == 403
    # Another organisation never sees it.
    other = await screening_world(owner_engine, recordings=False)
    assert (await api.call("GET", path, other, "recruiter")).status_code == 404
    # Overrides: people with the permission and a second factor, with a reason.
    body = {"scores": {"mentoring": 3}, "reason": "Led two interns, said in the interview."}
    assert (
        await api.call("POST", f"{path}/overrides", world, "interviewer", json=body)
    ).status_code == 403
    assert (
        await api.call("POST", f"{path}/overrides", world, "recruiter", otp=False, json=body)
    ).status_code == 403
    short = await api.call(
        "POST",
        f"{path}/overrides",
        world,
        "recruiter",
        json={"scores": {"mentoring": 3}, "reason": "ok"},
    )
    assert short.status_code in (400, 422)
    unknown = await api.call(
        "POST",
        f"{path}/overrides",
        world,
        "recruiter",
        json={"scores": {"nope": 3}, "reason": "A reason long enough."},
    )
    assert unknown.status_code == 409
    done = await api.call("POST", f"{path}/overrides", world, "recruiter", json=body)
    assert done.status_code == 200, done.text
    view = done.json()
    mentoring = next(c for c in view["criteria"] if c["criterion_id"] == "mentoring")
    assert mentoring["score"] == 3
    assert mentoring["overridden"] is True
    scores = {c["criterion_id"]: c["score"] for c in view["criteria"]}
    assert view["overall_score"] == overall_score(RUBRIC, scores)
    assert view["ai_overall_score"] == label("cv-0026").overall
    override_id = view["overrides"][-1]["decision_id"]
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT kind, actor_type, supersedes, sensitive IS NOT NULL AS has_reason "
        "FROM decision_records WHERE id = :i",
        {"i": override_id},
    )
    assert (row.kind, row.actor_type, row.has_reason) == ("human_override", "user", True)
    assert row.supersedes is not None
    # A human override is a person's decision, yet its result stays replayable: the
    # handler recomputes the overall score from the person's scores and the rubric.
    replayed = await api.call(
        "POST", f"/v1/decisions/{override_id}/replay", world, "admin", json={"mode": "recorded"}
    )
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["status"] == "match", replayed.text
    # Settings: everyone on the team reads them; only an admin changes them.
    assert (await api.call("GET", "/v1/hire/screening/settings", world, "recruiter")).json() == {
        "blind_mode": True,
        "blind_universities": True,
        "gap_months": 6,
        "classifier_mode": "always",
    }
    changed = {"blind_universities": False, "gap_months": 9}
    assert (
        await api.call("PUT", "/v1/hire/screening/settings", world, "recruiter", json=changed)
    ).status_code == 403
    saved = await api.call("PUT", "/v1/hire/screening/settings", world, "admin", json=changed)
    assert saved.status_code == 200
    assert saved.json()["gap_months"] == 9


async def test_registry_knows_the_screening_permissions() -> None:
    registry = build_registry(PLUGINS)
    for permission in (
        "hire.screening.read",
        "hire.screening.approve",
        "hire.screening.override",
        "hire.screening.propose",
        "hire.screening.manage",
    ):
        assert permission in registry

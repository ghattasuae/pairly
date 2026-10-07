# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""M1 acceptance: the expense note workflow end to end, on real Postgres, offline.

The model router runs in ``recorded`` mode with the shipped recordings bundle
(``pairly_expense/data/recordings.json``, imported by the ``expense-demo`` seed); its
providers are scripted fakes with no script, so any live call would fail the run. The
receipts are the committed synthetic PDFs (``synth/expense/receipts``). The scan uses
the fake scanner; parsing runs in the real sandbox.

1. A clean receipt: upload, parse, guardrails, sanitise, extraction with evidence, rules
   pass, risk low, interrupt, an approver decides over the API with a second factor,
   the executor marks the note reimbursable, decision records exist for extraction,
   rules, risk, approval and final action, the audit chain verifies and anchors.
2. A receipt with hidden white text: flagged, the hidden instruction never reaches the
   model input, the field that quotes it gets ``no_evidence``, risk needs a person and
   the approval still waits for one. The approver rejects; the submitter appeals.
3. Approval by signed email link: the approval email from the fake transport, describe
   (no state change), confirm as the bound user with a second factor.
4. 500 AED and more: two steps (approver, then finance controller) with segregation of
   duties.
5. ``pairly-admin seed expense-demo``: policies, invitations, recordings; idempotent.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from pairly_api.admin import run_seed
from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.audit import verify_chain
from pairly_core.audit_anchor.anchoring import anchor_map, anchor_org, load_anchors, public_keys
from pairly_core.audit_anchor.store import InMemoryAnchorStore
from pairly_core.config import Environment
from pairly_core.crypto.types import unseal
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.documents.pipeline import ParseConfig
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits
from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.scan import EICAR, FakeScanner
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.invitations import invite_as_system
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.settings import ModelMode, load_llm_settings
from pairly_core.models.llm import ModelCall
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.email import FakeEmailTransport
from pairly_core.plugins import SeedContext, WorkflowPlugin
from pairly_core.rbac import RoleRegistry, build_registry
from pairly_core.workflow.services import GraphServices
from pairly_expense.plugin import plugin as expense_plugin
from pairly_expense.seed import demo_email, seed_expense_demo
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin
from synth.expense.receipts import CLEAN, HIDDEN, HIDDEN_TEXT, THRESHOLD, Receipt, receipt_pdf

from .conftest import DbUrls, TokenFactory, make_settings
from .notify_support import WEB_URL, approval_hooks, link_token, notify_settings

PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin, expense_plugin]
REGISTRY: RoleRegistry = build_registry(PLUGINS)
RECEIPTS_DIR = Path(__file__).resolve().parents[3] / "synth" / "expense" / "receipts"
OWNER_URL_ENV = "PAIRLY_DATABASE_URL_OWNER"

# name -> memberships (role, workflow). Everyone is limited to the expense workflow but
# the outsider (pay only) and the clerk (every workflow).
PEOPLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "submitter": (("interviewer", "expense"),),
    "submitter2": (("interviewer", "expense"),),
    "submitter_approver": (("interviewer", "expense"), ("approver", "expense")),
    "approver": (("approver", "expense"),),
    "approver2": (("approver", "expense"),),
    "finance": (("finance_controller", "expense"),),
    "outsider": (("approver", "pay"),),
}


@dataclasses.dataclass
class World:
    org_id: uuid.UUID
    slug: str
    people: dict[str, tuple[uuid.UUID, str]] = dataclasses.field(default_factory=dict)

    def id(self, name: str) -> uuid.UUID:
        return self.people[name][0]


async def seed_world(owner: AsyncEngine) -> World:
    org_id = uuid.uuid4()
    world = World(org_id=org_id, slug=f"exp-{org_id.hex[:12]}")
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": org_id, "n": "Expense demo", "s": world.slug},
        )
        for name, memberships in PEOPLE.items():
            user_id = uuid.uuid4()
            subject = f"sub-exp-{name}-{user_id.hex[:10]}"
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
    # The demo seed: policies, approval rules and the recorded model responses.
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, org_id, None)
        async with session.begin():
            await seed_expense_demo(
                session, SeedContext(org_id, world.slug, REGISTRY, "https://app.test.pairly")
            )
    return world


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    engine: AsyncEngine
    store: InMemoryObjectStore

    def headers(
        self,
        world: World,
        name: str,
        *,
        otp: bool = True,
        org: bool = True,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        headers = {"Authorization": f"Bearer {self.tokens.token(world.people[name][1], **claims)}"}
        if org:
            headers["X-Pairly-Org"] = str(world.org_id)
        return headers | (extra or {})

    async def submit(
        self, world: World, name: str, receipt: Receipt, *, note: str = "Team lunch"
    ) -> httpx.Response:
        data = (RECEIPTS_DIR / receipt.filename).read_bytes()
        return await self.client.post(
            "/v1/expense/notes",
            files={"file": (receipt.filename, data, "application/pdf")},
            data={"note": note},
            headers=self.headers(world, name, extra={"Idempotency-Key": f"exp-{uuid.uuid4().hex}"}),
        )

    async def submit_bytes(
        self, world: World, name: str, filename: str, data: bytes
    ) -> dict[str, Any]:
        response = await self.client.post(
            "/v1/expense/notes",
            files={"file": (filename, data, "application/pdf")},
            headers=self.headers(world, name),
        )
        assert response.status_code == 202, response.text
        return await self.note(world, "approver", response.json()["id"])

    async def note(self, world: World, name: str, note_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"/v1/expense/notes/{note_id}", headers=self.headers(world, name)
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def decide(
        self,
        world: World,
        name: str,
        approval_id: str,
        decision: str = "approve",
        *,
        reason: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"decision": decision}
        if reason is not None:
            body["reason"] = reason
        return await self.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json=body,
            headers=self.headers(world, name, extra={"Idempotency-Key": f"d-{uuid.uuid4().hex}"}),
        )


def ingestion_settings() -> IngestionSettings:
    return IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
        parse_memory_mb=768,
        ocr_languages="eng",
    )


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
    store = InMemoryObjectStore()
    documents = DocumentServices(
        settings=ingestion_settings(), store=store, queue=RecordingJobQueue(), tmp_dir=tmp_path
    )
    engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    # Recorded mode only; the providers have no script, so a live call would fail.
    router = build_router(
        load_llm_settings(model_mode=ModelMode.RECORDED),
        create_sessionmaker(engine),
        providers={"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")},
    )
    services = GraphServices(
        store=store,
        router=router,
        scanner=FakeScanner(),
        parse_config=ParseConfig(
            options=ParseOptions(ocr_languages="eng"),
            limits=SandboxLimits(wall_seconds=60, memory_bytes=768 * 1024 * 1024),
        ),
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
    try:
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield Api(app, c, tokens, engine, store)
    finally:
        await engine.dispose()


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> World:
    return await seed_world(owner_engine)


def session_for(api: Api, org_id: uuid.UUID) -> AsyncSession:
    session = create_sessionmaker(api.engine)()
    set_tenant(session, org_id, None)
    return session


async def owner_rows(
    owner: AsyncEngine, org_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        return list((await conn.execute(text(sql), params or {})).all())


async def audit_actions(owner: AsyncEngine, org_id: uuid.UUID) -> list[str]:
    rows = await owner_rows(owner, org_id, "SELECT action FROM audit_events ORDER BY seq")
    return [r.action for r in rows]


async def record_kinds(owner: AsyncEngine, world: World, ids: list[str]) -> set[str]:
    rows = await owner_rows(
        owner, world.org_id, "SELECT kind FROM decision_records WHERE id = ANY(:ids)", {"ids": ids}
    )
    return {r.kind for r in rows}


async def submitted(api: Api, world: World, name: str, receipt: Receipt) -> dict[str, Any]:
    response = await api.submit(world, name, receipt)
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["status"] == "processing"
    return await api.note(world, "approver", created["id"])


def field(detail: dict[str, Any], path: str) -> dict[str, Any]:
    (found,) = [f for f in detail["extraction"]["fields"] if f["path"] == path]
    return dict(found)


# ------------------------------------------------------------------ 1. clean receipt


async def test_clean_receipt_end_to_end(api: Api, world: World, owner_engine: AsyncEngine) -> None:
    note = await submitted(api, world, "submitter", CLEAN)
    assert note["status"] == "awaiting_approval", note
    assert note["run_status"] == "waiting_approval"
    assert note["note"] == "Team lunch"
    assert note["document"]["status"] == "parsed"
    # Extraction from the recorded response, with evidence in the original receipt.
    extraction = note["extraction"]
    assert extraction["status"] == "ok", extraction["reasons"]
    assert extraction["from_recording"] is True
    total = field(note, "total")
    assert total["value"] == {"amount": "81.90", "currency": "AED"}
    assert total["flags"] == []
    assert total["evidence"][0]["quote"] == "Total: AED 81.90"
    assert field(note, "date")["value"] == "2026-10-06"
    assert field(note, "category")["value"] == "meals"
    assert (note["merchant"], note["amount"], note["currency"]) == ("Al Noor Cafe", "81.90", "AED")
    # Guardrails clean, every rule passes, risk does not force review.
    assert note["guardrails"]["findings"] == []
    assert {r["outcome"] for r in note["rules"]} == {"pass"}
    assert note["risk"]["requires_human"] is False
    assert note["review_reasons"] == []
    # Still a person decides: one approver for under 500 AED.
    approval = note["approval"]
    assert (approval["status"], approval["chain"]) == ("pending", "under_500")
    assert [s["name"] for s in approval["steps"]] == ["approver"]

    # The submitter cannot decide (no permission), the approver can, with a second factor.
    assert (await api.decide(world, "submitter", approval["id"])).status_code == 403
    no_otp = await api.client.post(
        f"/v1/approvals/{approval['id']}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "approver", otp=False, extra={"Idempotency-Key": "k-no-otp-1"}),
    )
    assert no_otp.status_code == 403
    decided = await api.decide(world, "approver", approval["id"], reason="Within policy")
    assert decided.status_code == 200, decided.text

    done = await api.note(world, "approver", note["id"])
    assert (done["status"], done["reimbursable"], done["run_status"]) == (
        "approved",
        True,
        "completed",
    )
    kinds = await record_kinds(owner_engine, world, done["decision_ids"])
    assert {"extraction", "policy_check", "risk_score", "approval", "final_action"} <= kinds

    # The submitter sees their own note and gets told.
    mine = await api.client.get("/v1/expense/notes", headers=api.headers(world, "submitter"))
    assert [n["id"] for n in mine.json()["items"]] == [note["id"]]
    other = await api.client.get(
        f"/v1/expense/notes/{note['id']}", headers=api.headers(world, "submitter2")
    )
    assert other.status_code == 404
    outsider = await api.client.get("/v1/expense/notes", headers=api.headers(world, "outsider"))
    assert outsider.status_code == 403
    told = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT channel, status FROM notifications WHERE template_key = 'expense.note.decided' "
        "AND recipient_user_id = :u",
        {"u": world.id("submitter")},
    )
    assert ("in_app", "sent") in {(r.channel, r.status) for r in told}

    actions = await audit_actions(owner_engine, world.org_id)
    for action in (
        "expense.note.submitted",
        "core.document.parsed",
        "expense.note.guardrails_scanned",
        "expense.note.sanitized",
        "core.extraction.created",
        "core.policy.rules_evaluated",
        "core.risk.assessed",
        "core.approval.requested",
        "core.workflow.action_executed",
        "expense.note.approved",
        "core.workflow.run_completed",
    ):
        assert action in actions, action

    # The audit chain verifies, and against a signed anchor of its head.
    key = Ed25519PrivateKey.generate()
    store = InMemoryAnchorStore()
    async with session_for(api, world.org_id) as s, s.begin():
        assert (await verify_chain(s, world.org_id)).ok
        assert (await anchor_org(s, store, key)).status == "written"
    anchors = anchor_map(await load_anchors(store, world.org_id, public_keys(key.public_key())))
    async with session_for(api, world.org_id) as s, s.begin():
        assert (await verify_chain(s, world.org_id, anchors=anchors)).ok


async def test_submission_is_idempotent_and_checked(api: Api, world: World) -> None:
    data = (RECEIPTS_DIR / CLEAN.filename).read_bytes()
    headers = api.headers(world, "submitter", extra={"Idempotency-Key": "expense-retry-0001"})
    files = {"file": (CLEAN.filename, data, "application/pdf")}
    first = await api.client.post("/v1/expense/notes", files=files, headers=headers)
    again = await api.client.post("/v1/expense/notes", files=files, headers=headers)
    assert first.status_code == again.status_code == 202
    assert first.json()["id"] == again.json()["id"]
    listed = await api.client.get("/v1/expense/notes", headers=api.headers(world, "approver"))
    assert len(listed.json()["items"]) == 1
    # People without expense.note.submit, and notes that are too long, are refused.
    refused = await api.client.post(
        "/v1/expense/notes", files=files, headers=api.headers(world, "outsider")
    )
    assert refused.status_code == 403
    long_note = await api.client.post(
        "/v1/expense/notes",
        files=files,
        data={"note": "x" * 1001},
        headers=api.headers(world, "submitter"),
    )
    assert long_note.status_code == 400


# ------------------------------------------------------------------ 2. hidden text


async def test_hidden_text_is_flagged_and_a_person_still_decides(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    note = await submitted(api, world, "submitter", HIDDEN)
    assert note["status"] == "awaiting_approval"
    guardrails = note["guardrails"]
    assert guardrails["flagged"] is True
    hidden = [f for f in guardrails["findings"] if f["type"] == "hidden_text"]
    assert hidden
    assert hidden[0]["removed_from_model_input"] is True
    assert hidden[0]["evidence"] == HIDDEN_TEXT
    assert guardrails["removed_spans"] >= 1
    # The field that quotes the hidden line gets no evidence and no credit.
    description = field(note, "description")
    assert description["flags"] == ["no_evidence"]
    assert description["confidence"] == 0.0
    assert note["extraction"]["status"] == "needs_human"
    # Visible fields still have evidence.
    assert field(note, "total")["evidence"][0]["quote"] == "Total: AED 145.00"
    # Risk needs a person: a critical guardrail signal.
    assert note["risk"]["requires_human"] is True
    assert "critical_signal:core.guardrail_findings" in note["risk"]["reasons"]
    assert any(r.startswith("guardrails_flagged:") for r in note["review_reasons"])
    assert note["approval"]["status"] == "pending"

    # The hidden instruction never reached the model: the replayed calls' inputs.
    async with session_for(api, world.org_id) as s, s.begin():
        calls = list(
            await s.scalars(
                select(ModelCall).where(
                    ModelCall.task == "core_extract", ModelCall.mode == "recorded"
                )
            )
        )
    assert calls
    for call in calls:
        assert HIDDEN_TEXT not in json.dumps(unseal(call, "request"), ensure_ascii=False)

    # The submitter's view keeps the evidence text and the signals for reviewers.
    own = await api.note(world, "submitter", note["id"])
    assert own["guardrails"]["findings"][0]["evidence"] is None
    assert own["risk"]["signals"] is None

    # A person decides: rejected, with a reason. Nothing is marked reimbursable.
    rejected = await api.decide(
        world, "approver", note["approval"]["id"], "reject", reason="Hidden text"
    )
    assert rejected.status_code == 200, rejected.text
    closed = await api.note(world, "approver", note["id"])
    assert (closed["status"], closed["reimbursable"]) == ("rejected", False)
    assert "core.workflow.action_executed" not in await audit_actions(owner_engine, world.org_id)

    # The submitter contests the approval decision (expense.appeal.create).
    records = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM decision_records WHERE id = ANY(:ids) AND kind = 'approval'",
        {"ids": closed["decision_ids"]},
    )
    (record,) = records
    appeal = await api.client.post(
        f"/v1/decisions/{record.id}/appeals",
        json={
            "requester_type": "staff",
            "requester_id": str(world.id("submitter")),
            "reason": "The receipt is mine and real, please look again",
        },
        headers=api.headers(world, "submitter"),
    )
    assert appeal.status_code == 201, appeal.text
    # Someone who cannot read expense records cannot file one.
    foreign = await api.client.post(
        f"/v1/decisions/{record.id}/appeals",
        json={"requester_type": "vendor", "requester_id": "v", "reason": "x"},
        headers=api.headers(world, "outsider"),
    )
    assert foreign.status_code == 404


# ------------------------------------------------------------------ 3. signed email link

_LINK_RE = re.compile(re.escape(WEB_URL) + r"/approvals/link#[A-Za-z0-9_-]{43}")


async def test_approve_from_a_signed_email_link(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    note = await submitted(api, world, "submitter", CLEAN)
    fake = FakeEmailTransport()
    settings = notify_settings()
    stats = await deliver_org(
        create_sessionmaker(api.engine),
        world.org_id,
        Transports(settings=settings, email=fake, slack=FakeSlackTransport()),
    )
    assert stats.sent >= 1
    (email,) = [m for m in fake.sent if m.to.startswith("approver-")]
    approve_url, reject_url = _LINK_RE.findall(email.text)[:2]  # Approve, then Reject
    token = link_token(approve_url)

    bound = api.headers(world, "approver", org=False)
    described = await api.client.post(
        "/v1/approval-links/describe", json={"token": token}, headers=bound
    )
    assert described.status_code == 200, described.text
    assert (described.json()["action"], described.json()["state"]) == ("approve", "valid")
    # Describing changes nothing.
    still = await api.note(world, "approver", note["id"])
    assert (still["status"], still["approval"]["status"]) == ("awaiting_approval", "pending")

    # Bound to its user: another approver gets the same refusal as an unknown link.
    other = await api.client.post(
        "/v1/approval-links/confirm",
        json={"token": token},
        headers=api.headers(world, "approver2", org=False),
    )
    assert other.status_code == 410
    confirmed = await api.client.post(
        "/v1/approval-links/confirm", json={"token": token}, headers=bound
    )
    assert confirmed.status_code == 200, confirmed.text
    done = await api.note(world, "approver", note["id"])
    assert (done["status"], done["reimbursable"]) == ("approved", True)
    assert done["approval"]["decisions"][0]["channel"] == "email_link"
    # Single use: confirming again returns the same decision, records nothing new, and
    # the other link of the email (Reject) is dead.
    replay = await api.client.post(
        "/v1/approval-links/confirm", json={"token": token}, headers=bound
    )
    assert replay.status_code == 200
    assert replay.json()["decision_id"] == confirmed.json()["decision_id"]
    reject = await api.client.post(
        "/v1/approval-links/confirm", json={"token": link_token(reject_url)}, headers=bound
    )
    assert reject.status_code == 410
    final = await api.note(world, "approver", note["id"])
    assert len(final["approval"]["decisions"]) == 1
    async with session_for(api, world.org_id) as s, s.begin():
        assert (await verify_chain(s, world.org_id)).ok


# ------------------------------------------------------------------ 4. two steps, SoD


async def test_two_step_chain_with_segregation_of_duties(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    note = await submitted(api, world, "submitter_approver", THRESHOLD)
    assert (note["amount"], note["currency"]) == ("1250.00", "AED")
    assert {r["outcome"] for r in note["rules"]} == {"pass"}
    approval = note["approval"]
    assert approval["chain"] == "from_500"
    assert [s["name"] for s in approval["steps"]] == ["approver", "finance"]
    approval_id = approval["id"]

    # The submitter holds the approver role, but never approves their own note.
    own = await api.decide(world, "submitter_approver", approval_id)
    assert own.status_code == 403, own.text
    # The finance controller cannot take the approver's step.
    early = await api.decide(world, "finance", approval_id)
    assert early.status_code == 403, early.text
    first = await api.decide(world, "approver", approval_id)
    assert first.status_code == 200, first.text
    half = await api.note(world, "approver", note["id"])
    assert (half["status"], half["approval"]["status"]) == ("awaiting_approval", "pending")
    # The same person cannot also take the second step.
    twice = await api.decide(world, "approver", approval_id)
    assert twice.status_code == 403, twice.text
    second = await api.decide(world, "finance", approval_id)
    assert second.status_code == 200, second.text

    done = await api.note(world, "approver", note["id"])
    assert (done["status"], done["reimbursable"]) == ("approved", True)
    deciders = {d["decided_by"] for d in done["approval"]["decisions"]}
    assert deciders == {str(world.id("approver")), str(world.id("finance"))}
    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("core.workflow.action_executed") == 1


# ------------------------------------------------------------------ fail closed


async def test_no_recorded_response_fails_closed_to_a_person(api: Api, world: World) -> None:
    """A receipt the recordings do not cover: the model is unavailable in recorded mode,
    so nothing is read, every check that needs a value errors, and the note waits for
    two people (the amount is unknown, so the larger chain applies)."""
    unknown = Receipt(
        key="unknown",
        filename="receipt-unknown.pdf",
        lines=("Corner Bakery", "Date: 06/10/2026", "Total: AED 42.00"),
        answers=(),
    )
    note = await api.submit_bytes(world, "submitter", unknown.filename, receipt_pdf(unknown))
    assert note["status"] == "awaiting_approval"
    assert note["extraction"]["status"] == "needs_human"
    assert any(r.startswith("extraction:model_unavailable") for r in note["review_reasons"])
    assert note["amount"] is None
    assert "error" in {r["outcome"] for r in note["rules"]}
    assert note["risk"]["requires_human"] is True
    assert note["approval"]["chain"] == "from_500"


async def test_unreadable_or_infected_receipts_go_to_a_person(api: Api, world: World) -> None:
    broken = await api.submit_bytes(
        world, "submitter", "broken.pdf", b"%PDF-1.7\n" + b"not really a pdf " * 64
    )
    assert broken["status"] == "awaiting_approval"
    assert broken["document"]["status"] == "parse_failed"
    assert any(r.startswith("document_not_parsed:") for r in broken["review_reasons"])
    assert broken["extraction"] is None
    assert broken["guardrails"] is None
    assert broken["risk"]["requires_human"] is True
    assert "detector_error:core.guardrail_findings" in broken["risk"]["reasons"]
    assert broken["approval"]["chain"] == "from_500"

    infected = await api.submit_bytes(
        world, "submitter", "infected.pdf", b"%PDF-1.7\n" + EICAR + b"\n%%EOF\n"
    )
    assert infected["document"]["status"] == "infected"
    assert "document_not_parsed:infected" in infected["review_reasons"]
    assert infected["approval"]["status"] == "pending"


# ------------------------------------------------------------------ 5. the seed command


async def test_seed_command_is_idempotent(
    migrated: DbUrls, owner_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = uuid.uuid4()
    slug = f"seed-{org_id.hex[:12]}"
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, 'Seed', :s)"),
            {"id": org_id, "s": slug},
        )
    first = await run_seed(migrated.owner, "expense-demo", slug, "https://app.test.pairly")
    assert {i.email for i in first.invitations} == {
        demo_email(slug, who) for who in ("submitter", "approver", "finance")
    }
    assert {i.workflow for i in first.invitations} == {"expense"}
    assert any("3 imported" in line for line in first.lines)
    again = await run_seed(migrated.owner, "expense-demo", slug, "https://app.test.pairly")
    assert again.invitations == ()
    assert any("0 imported, 3 already there" in line for line in again.lines)
    rows = await owner_rows(
        owner_engine,
        org_id,
        "SELECT kind, name, version FROM policy_versions ORDER BY kind",
    )
    assert {(r.kind, r.name, r.version) for r in rows} == {
        ("approval_rules", "expense.note", 1),
        ("risk_policy", "expense.note", 1),
        ("rules", "expense.note", 1),
    }
    with pytest.raises(LookupError):
        await run_seed(migrated.owner, "expense-demo", "no-such-org", "https://app.test.pairly")
    # System invitations refuse the agent role, unknown roles and unknown workflows.
    session = create_sessionmaker(owner_engine)()
    async with session:
        with pytest.raises(ForbiddenError):
            async with session.begin():
                await invite_as_system(
                    session,
                    REGISTRY,
                    email="x@example.test",
                    role="approver",
                    workflow=None,
                    actor_id="test",
                )
        set_tenant(session, org_id, None)
        for role, workflow in (("agent", None), ("nope", None), ("approver", "nope")):
            with pytest.raises(BadRequestError):
                async with session.begin():
                    await invite_as_system(
                        session,
                        REGISTRY,
                        email="x@example.test",
                        role=role,
                        workflow=workflow,
                        actor_id="test",
                    )
    with pytest.raises(LookupError):
        await run_seed(migrated.owner, "nope-demo", slug, "https://app.test.pairly")

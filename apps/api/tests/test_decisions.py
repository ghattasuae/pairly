# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision records, replay, receipts, appeals and compliance packs on real Postgres.

The model router is real (``model_calls`` rows are written) with scripted fake
providers; nothing leaves the process and live replays are never run. All data is
synthetic.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pdfplumber
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from pairly_api.decision_services import DecisionServices
from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.audit import ActorType, verify_chain
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.decisions.appeals import create_appeal, escalate_overdue_appeals
from pairly_core.decisions.compliance import compliance_key, manifest_files, run_export
from pairly_core.decisions.ids import is_decision_id
from pairly_core.decisions.jobs import COMPLIANCE_EXPORT_JOB, RecordingTaskQueue
from pairly_core.decisions.models import DecisionRecord, RequesterType
from pairly_core.decisions.replay import replay_decision
from pairly_core.decisions.service import history, open_sensitive, verify_record
from pairly_core.extraction.service import correct_field
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.llm.budget import BudgetManager
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.resilience import RetryPolicy
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.store import DatabaseCallStore
from pairly_core.models import AuditEvent
from pairly_core.policy.service import Subject, evaluate_and_record, publish_rule_set
from pairly_core.rbac import Actor, ActorKind, RoleGrant, build_registry
from pairly_core.risk.service import assess
from pairly_core.risk.types import RiskContext, Severity

from .conftest import DbUrls, TokenFactory, make_settings
from .test_documents import ingestion_settings
from .test_extraction_risk import (
    PLUGINS,
    FixedDetector,
    publish,
    run_extraction,
    signal,
)

REGISTRY = build_registry(PLUGINS)
PEOPLE: dict[str, str] = {
    "admin": "admin",
    "auditor": "auditor",
    "clerk": "ap_clerk",
    "approver": "approver",
    "fc": "finance_controller",
    "reader": "read_only",
    # Memberships limited to one workflow (WORKFLOW_SCOPED).
    "pay_approver": "approver",
    "hire_manager": "hiring_manager",
}
WORKFLOW_SCOPED: dict[str, str] = {"pay_approver": "pay", "hire_manager": "hire"}
RULES = """\
schema: pairly.rules/v1
name: pay.spend
facts:
  amount: decimal
  category: string
rules:
  - id: big_spend
    description:
      en: Spend over 100,000 AED is blocked for review
      ar: الإنفاق الكبير يتوقف للمراجعة
    when: "amount > 100000"
    outcome: fail
    reason: "Spend of {amount} AED is over the limit"
"""


@dataclasses.dataclass
class Org:
    org_id: uuid.UUID
    people: dict[str, tuple[uuid.UUID, str]] = dataclasses.field(default_factory=dict)
    documents: dict[str, uuid.UUID] = dataclasses.field(default_factory=dict)

    def user(self, name: str) -> uuid.UUID:
        return self.people[name][0]

    def actor(self, name: str, *, otp: bool = True) -> Actor:
        grants = (RoleGrant(PEOPLE[name], WORKFLOW_SCOPED.get(name)),)
        return Actor(
            kind=ActorKind.USER,
            id=self.user(name),
            org_id=self.org_id,
            grants=grants,
            permissions=REGISTRY.resolve(grants),
            subject=self.people[name][1],
            amr=frozenset({"otp"}) if otp else frozenset(),
        )


async def seed_org(owner: AsyncEngine, label: str) -> Org:
    org = Org(org_id=uuid.uuid4())
    async with owner.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.org_id)}
        )
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": org.org_id, "n": f"Dec {label}", "s": f"dec-{label}-{org.org_id.hex[:10]}"},
        )
        for name, role in PEOPLE.items():
            user_id = uuid.uuid4()
            subject = f"sub-dec-{name}-{user_id.hex[:10]}"
            await conn.execute(
                text("SELECT set_config('app.auth_subject', :s, true)"), {"s": subject}
            )
            await conn.execute(
                text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
                {"id": user_id, "s": subject, "e": f"{name}-{user_id.hex[:6]}@example.test"},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (org_id, user_id, role_id, workflow) "
                    "SELECT :o, :u, r.id, :wf FROM roles r "
                    "WHERE r.key = :role AND r.org_id IS NULL"
                ),
                {"o": org.org_id, "u": user_id, "role": role, "wf": WORKFLOW_SCOPED.get(name)},
            )
            org.people[name] = (user_id, subject)
        doc_id = uuid.uuid4()
        sha = hashlib.sha256(f"{label}-{doc_id}".encode()).hexdigest()
        await conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workflow, source, media_type, size_bytes, "
                "sha256, storage_key, status) VALUES "
                "(:id, :o, 'expense', 'upload', 'application/pdf', 10, :sha, :key, 'parsed')"
            ),
            {"id": doc_id, "o": org.org_id, "sha": sha, "key": f"k/{sha}"},
        )
        org.documents["expense"] = doc_id
    return org


@pytest.fixture
async def engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    yield eng
    await eng.dispose()


@pytest.fixture
async def org_a(owner_engine: AsyncEngine) -> Org:
    return await seed_org(owner_engine, "a")


@pytest.fixture
async def org_b(owner_engine: AsyncEngine) -> Org:
    return await seed_org(owner_engine, "b")


def session_for(engine: AsyncEngine, org: uuid.UUID) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, None)
    return session


def recorded_router(engine: AsyncEngine) -> ModelRouter:
    """Replays only stored responses (``recorded`` mode): live calls are impossible."""
    prices = load_prices()
    return ModelRouter(
        settings=LLMSettings(model_mode=ModelMode.RECORDED),
        providers={
            "anthropic": FakeProvider("anthropic", available=False),
            "openai": FakeProvider("openai", available=False),
        },
        catalog=load_catalog(prices),
        prices=prices,
        prompts=PromptRegistry(),
        sessionmaker=create_sessionmaker(engine),
        budget=BudgetManager(default_limit_micros=10_000_000),
        store=DatabaseCallStore(),
        retry_policy=RetryPolicy(max_retries=0),
    )


async def owner_sql(
    owner: AsyncEngine, org: uuid.UUID, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def records(engine: AsyncEngine, org: Org, **where: str) -> list[DecisionRecord]:
    async with session_for(engine, org.org_id) as s, s.begin():
        query = select(DecisionRecord).order_by(DecisionRecord.created_at, DecisionRecord.id)
        for column, value in where.items():
            query = query.where(getattr(DecisionRecord, column) == value)
        return list(await s.scalars(query))


async def stored_extraction(engine: AsyncEngine, org: Org) -> tuple[uuid.UUID, DecisionRecord]:
    result = await run_extraction(engine, org)  # type: ignore[arg-type]
    from pairly_core.extraction.service import save_extraction

    async with session_for(engine, org.org_id) as s, s.begin():
        extraction_id = await save_extraction(
            s,
            result,
            document_id=org.documents["expense"],
            actor_type=ActorType.SYSTEM,
            actor_id="extractor",
        )
    (record,) = await records(engine, org, kind="extraction")
    return extraction_id, record


async def rules_record(engine: AsyncEngine, org: Org, amount: str = "150000") -> DecisionRecord:
    async with session_for(engine, org.org_id) as s, s.begin():
        await publish_rule_set(
            s, name="pay.spend", content=RULES, actor_type=ActorType.SYSTEM, actor_id="tests"
        )
    async with session_for(engine, org.org_id) as s, s.begin():
        await evaluate_and_record(
            s,
            name="pay.spend",
            facts={"amount": Decimal(amount), "category": "travel"},
            actor_type=ActorType.SYSTEM,
            actor_id="policy",
            subject=Subject(type="invoice", id="inv-7", workflow="pay"),
            as_of=date(2026, 10, 7),
        )
    return (await records(engine, org, kind="policy_check"))[-1]


async def risk_record(engine: AsyncEngine, org: Org) -> DecisionRecord:
    await publish(engine, org)  # type: ignore[arg-type]
    detectors = [
        FixedDetector(signal("core.amount_outlier", Severity.MEDIUM, 0.8)),
        FixedDetector(signal("core.extraction_confidence", Severity.LOW, 0.1)),
    ]
    async with session_for(engine, org.org_id) as s, s.begin():
        await assess(
            s,
            RiskContext(
                org_id=org.org_id,
                workflow="expense",
                entity_type="expense_claim",
                entity_id="claim-1",
                document_sha256="a" * 64,
            ),
            detectors,
            assessment_type="expense.claim",
            actor_type=ActorType.SYSTEM,
            actor_id="risk",
        )
    (record,) = await records(engine, org, kind="risk_score")
    return record


async def replay(
    engine: AsyncEngine, org: Org, record: DecisionRecord, *, live: bool = False
) -> Any:
    async with session_for(engine, org.org_id) as s, s.begin():
        row = await s.scalar(select(DecisionRecord).where(DecisionRecord.id == record.id))
        assert row is not None
        return await replay_decision(
            s,
            recorded_router(engine),
            row,
            live=live,
            actor_type=ActorType.USER,
            actor_id=str(org.user("auditor")),
        )


# ------------------------------------------------------------------ records from hooks


async def test_extraction_writes_a_linked_encrypted_record(
    engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    extraction_id, record = await stored_extraction(engine, org_a)
    assert is_decision_id(record.id)
    assert (record.workflow, record.entity_type) == ("expense", "document")
    assert record.entity_id == str(org_a.documents["expense"])
    assert record.inputs["extraction_id"] == str(extraction_id)
    assert len(record.inputs["document_hashes"][0]) == 64
    assert record.model_call_ids
    calls = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT task, prompt_version, prompt_hash FROM model_calls WHERE id = ANY(:ids)",
        {"ids": list(record.model_call_ids)},
    )
    assert record.prompt_versions == [
        {"task": calls[0].task, "version": calls[0].prompt_version, "hash": calls[0].prompt_hash}
    ]
    assert record.replay_handler == "core.extraction"
    # Personal data is encrypted at rest and only in the sensitive part.
    (raw,) = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT sensitive, output::text AS output FROM decision_records WHERE id = :id",
        {"id": record.id},
    )
    assert raw.sensitive.startswith("v1:")
    assert "INV-2026-0042" not in raw.sensitive
    assert "INV-2026-0042" not in raw.output
    assert open_sensitive(record)["fields"]["invoice_number"] == "INV-2026-0042"
    # Linked to the extraction's audit event, and its hash is in the chain.
    created = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT id FROM audit_events WHERE action = 'core.extraction.created'",
    )
    assert list(record.audit_event_ids) == [created[0].id]
    async with session_for(engine, org_a.org_id) as s, s.begin():
        assert await verify_record(s, record) is None
        assert (await verify_chain(s, org_a.org_id)).ok


async def test_correction_supersedes_the_extraction_record(engine: AsyncEngine, org_a: Org) -> None:
    extraction_id, record = await stored_extraction(engine, org_a)
    async with session_for(engine, org_a.org_id) as s, s.begin():
        await correct_field(
            s,
            org_a.actor("admin"),
            REGISTRY,
            extraction_id,
            field_path="invoice_number",
            new_value="INV-2026-0043",
            reason="Typo on the scan",
        )
    (correction,) = await records(engine, org_a, kind="extraction_correction")
    assert correction.supersedes == record.id
    assert correction.final_action == "corrected"
    assert correction.actor_id == str(org_a.user("admin"))
    sensitive = open_sensitive(correction)
    assert sensitive["new_value"] == "INV-2026-0043"
    assert sensitive["reason"] == "Typo on the scan"
    async with session_for(engine, org_a.org_id) as s, s.begin():
        assert await history(s, record) == [record.id, correction.id]


async def test_risk_and_rules_write_records_with_policy_versions(
    engine: AsyncEngine, org_a: Org
) -> None:
    risk = await risk_record(engine, org_a)
    assert risk.entity_id == "claim-1"
    (policy,) = risk.policy_versions
    assert (policy["kind"], policy["name"], policy["version"]) == (
        "risk_policy",
        "expense.claim",
        1,
    )
    assert len(policy["hash"]) == 64
    assert {s["detector"] for s in risk.evidence["risk_signals"]} == {
        "core.amount_outlier",
        "core.extraction_confidence",
    }
    rules = await rules_record(engine, org_a)
    assert (rules.workflow, rules.entity_type, rules.entity_id) == ("pay", "invoice", "inv-7")
    assert rules.output["overall"] == "fail"
    assert rules.policy_versions[0]["name"] == "pay.spend"
    facts = open_sensitive(rules)["facts"]
    assert facts["amount"] == {"__pairly_type__": "decimal", "v": "150000"}


async def test_records_are_append_only(
    engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    record = await rules_record(engine, org_a)
    for sql in (
        "UPDATE decision_records SET output = '{}'::jsonb",
        "DELETE FROM decision_records",
        "TRUNCATE decision_records CASCADE",
    ):
        async with session_for(engine, org_a.org_id) as s:
            with pytest.raises(DBAPIError):
                async with s.begin():
                    await s.execute(text(sql))
    # Not even the owner may change content ...
    with pytest.raises(DBAPIError, match="append-only"):
        await owner_sql(
            owner_engine,
            org_a.org_id,
            "UPDATE decision_records SET final_action = 'x' WHERE id = :id",
            {"id": record.id},
        )
    # ... only re-wrap the encrypted column (key rotation).
    await owner_sql(
        owner_engine,
        org_a.org_id,
        "UPDATE decision_records SET sensitive = sensitive WHERE id = :id",
        {"id": record.id},
    )


# ------------------------------------------------------------------ replay


async def test_replay_matches_for_every_core_handler(engine: AsyncEngine, org_a: Org) -> None:
    _, extraction = await stored_extraction(engine, org_a)
    risk = await risk_record(engine, org_a)
    rules = await rules_record(engine, org_a)
    for record in (extraction, risk, rules):
        outcome = await replay(engine, org_a, record)
        assert (outcome.status.value, outcome.reason) == ("match", None), outcome.details
        assert outcome.deterministic
        assert outcome.diff == []
    # The original never changes.
    for before in (extraction, risk, rules):
        (after,) = await records(engine, org_a, id=before.id)
        assert after.content_hash == before.content_hash


async def test_replay_differs_with_a_fingerprinted_field_diff(
    engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    _, record = await stored_extraction(engine, org_a)
    # A newer stored response for the same request (as a live re-run that answered
    # differently would leave): the recorded replay now sees another output.
    call_id = record.model_call_ids[-1]
    async with session_for(engine, org_a.org_id) as s, s.begin():
        original = await DatabaseCallStore().get(s, call_id)
    assert original is not None
    changed = json.loads(json.dumps(original.response))
    changed["fields"][0]["value"] = "INV-2026-9999"
    async with session_for(engine, org_a.org_id) as s, s.begin():
        await DatabaseCallStore().insert(
            s,
            {
                "org_id": org_a.org_id,
                "chain_id": uuid.uuid4(),
                "attempt": 1,
                "task": original.task,
                "prompt_version": original.prompt_version,
                "prompt_hash": original.prompt_hash,
                "provider": original.provider,
                "model": original.model,
                "tier": original.tier,
                "schema_name": original.schema_name,
                "schema_hash": original.schema_hash,
                "request_hash": original.request_hash,
                "mode": "record",
                "status": "ok",
                "request": original.request,
                "response": changed,
                "output_complete": True,
                "confidence": original.confidence,
            },
        )
    outcome = await replay(engine, org_a, record)
    assert outcome.status.value == "differs"
    paths = {d["path"] for d in outcome.diff}
    assert f"model[{call_id}].fields[0].value" in paths
    assert "recomputed.invoice_number" in paths
    # Personal values are never stored in the diff, only fingerprints.
    assert "INV-2026-9999" not in json.dumps(outcome.diff)
    assert all(d.get("fingerprinted") for d in outcome.diff)


async def test_replay_fails_closed(
    engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    _, record = await stored_extraction(engine, org_a)
    # Live replay in recorded mode cannot run.
    live = await replay(engine, org_a, record, live=True)
    assert live.status.value == "failed"
    assert live.reason.startswith("model_replay_unavailable")
    assert live.mode.value == "live"
    # A human decision cannot be re-run.
    extraction_id = uuid.UUID(record.inputs["extraction_id"])
    async with session_for(engine, org_a.org_id) as s, s.begin():
        await correct_field(
            s,
            org_a.actor("admin"),
            REGISTRY,
            extraction_id,
            field_path="invoice_number",
            new_value="INV-1",
            reason="fix",
        )
    (human,) = await records(engine, org_a, kind="extraction_correction")
    assert (await replay(engine, org_a, human)).reason == "human_decision_not_replayable"
    # A record changed behind the database's back is not intact.
    rules = await rules_record(engine, org_a)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_a.org_id)}
        )
        await conn.execute(
            text("ALTER TABLE decision_records DISABLE TRIGGER decision_records_immutable")
        )
        await conn.execute(
            text(
                "UPDATE decision_records SET output = jsonb_set(output, '{overall}', "
                "'\"pass\"') WHERE id = :id"
            ),
            {"id": rules.id},
        )
        await conn.execute(
            text("ALTER TABLE decision_records ENABLE ALWAYS TRIGGER decision_records_immutable")
        )
    tampered = await replay(engine, org_a, rules)
    assert tampered.status.value == "failed"
    assert tampered.reason == "record_not_intact:content_hash_mismatch"
    # Missing recordings fail too.
    risk = await risk_record(engine, org_a)
    await owner_sql(owner_engine, org_a.org_id, "DELETE FROM model_calls")
    gone = await replay(engine, org_a, record)
    assert gone.reason == "model_call_missing"
    assert (await replay(engine, org_a, risk)).status.value == "match"
    rows = await owner_sql(
        owner_engine, org_a.org_id, "SELECT status, reason FROM decision_replays"
    )
    assert sum(1 for r in rows if r.status == "failed") == 4
    # Replay rows are append-only.
    async with session_for(engine, org_a.org_id) as s:
        with pytest.raises(DBAPIError):
            async with s.begin():
                await s.execute(text("UPDATE decision_replays SET status = 'match'"))


# ------------------------------------------------------------------ API


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    queue: RecordingTaskQueue
    store: InMemoryObjectStore

    def headers(self, org: Org, name: str, *, otp: bool = True) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        return {
            "Authorization": f"Bearer {self.tokens.token(org.people[name][1], **claims)}",
            "X-Pairly-Org": str(org.org_id),
        }


@pytest.fixture
async def api(
    migrated: DbUrls, tokens: TokenFactory, engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Api]:
    queue = RecordingTaskQueue()
    store = InMemoryObjectStore()
    documents = DocumentServices(
        settings=ingestion_settings(),
        store=store,
        queue=RecordingJobQueue(),
        tmp_dir=tmp_path,
    )
    app = create_app(
        make_settings(migrated.app),
        plugins=PLUGINS,
        documents=documents,
        decision_services=DecisionServices(router=recorded_router(engine), queue=queue),
    )
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens, queue, store)


async def test_receipts_are_redacted_by_role(
    api: Api, engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    _, record = await stored_extraction(engine, org_a)
    url = f"/v1/decisions/{record.id}/receipt"
    full = (await api.client.get(url, headers=api.headers(org_a, "admin"))).json()
    assert full["audience"] == "internal_full"
    assert full["personal_data"]["fields"]["invoice_number"] == "INV-2026-0042"
    assert full["integrity"]["intact"] is True
    viewed = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT entity_id FROM audit_events WHERE action = 'core.decision.personal_data_viewed'",
    )
    assert [v.entity_id for v in viewed] == [record.id]

    audit = (await api.client.get(url, headers=api.headers(org_a, "auditor"))).json()
    assert audit["audience"] == "internal"
    assert audit["personal_data"]["fields"] == {"redacted": True}
    assert "personal_data.fields" in audit["redacted"]
    assert audit["model_call_ids"]
    assert "INV-2026-0042" not in json.dumps(audit)

    # Read only holds no workflow read permission and no core-wide grant: an audited 403.
    reader = await api.client.get(url, headers=api.headers(org_a, "reader"))
    assert reader.status_code == 403
    # An AP clerk reads pay, not the expense workflow of this record: it does not exist.
    clerk = await api.client.get(url, headers=api.headers(org_a, "clerk"))
    assert clerk.status_code == 404

    pdf_en = await api.client.get(url + ".pdf", headers=api.headers(org_a, "auditor"))
    assert pdf_en.headers["content-type"] == "application/pdf"
    with pdfplumber.open(io.BytesIO(pdf_en.content)) as doc:
        text_en = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "Decision receipt" in text_en
    assert record.id in text_en
    assert "INV-2026-0042" not in text_en


async def test_arabic_pdf_is_shaped_and_right_to_left(
    api: Api, engine: AsyncEngine, org_a: Org
) -> None:
    record = await rules_record(engine, org_a)
    response = await api.client.get(
        f"/v1/decisions/{record.id}/receipt.pdf?lang=ar", headers=api.headers(org_a, "admin")
    )
    assert response.status_code == 200
    with pdfplumber.open(io.BytesIO(response.content)) as doc:
        extracted = "\n".join(p.extract_text() or "" for p in doc.pages)
    # Glyphs are presentation forms in visual (right-to-left) order.
    assert any("ﹰ" <= c <= "﻿" for c in extracted)
    folded = unicodedata.normalize("NFKC", extracted)
    assert "إيصال القرار"[::-1] in folded
    assert "النتيجة"[::-1] in folded
    assert record.id in folded
    assert "big_spend" in folded


async def test_replay_api_and_permissions(api: Api, engine: AsyncEngine, org_a: Org) -> None:
    record = await rules_record(engine, org_a)
    url = f"/v1/decisions/{record.id}/replay"
    for name in ("clerk", "approver", "reader"):
        denied = await api.client.post(url, json={}, headers=api.headers(org_a, name))
        assert denied.status_code == 403
    done = await api.client.post(url, json={}, headers=api.headers(org_a, "auditor"))
    assert done.status_code == 201, done.text
    assert done.json()["status"] == "match"
    # Pay readers (an AP clerk, a pay-only approver) see the pay record's replays.
    for name in ("clerk", "pay_approver"):
        listed = await api.client.get(
            f"/v1/decisions/{record.id}/replays", headers=api.headers(org_a, name)
        )
        assert [r["status"] for r in listed.json()] == ["match"]
    found = await api.client.get(
        "/v1/decisions?entity_type=invoice&entity_id=inv-7", headers=api.headers(org_a, "clerk")
    )
    assert [d["id"] for d in found.json()] == [record.id]


# ------------------------------------------------------------------ appeals


async def test_appeal_lifecycle(
    api: Api, engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    record = await rules_record(engine, org_a)
    filed = await api.client.post(
        f"/v1/decisions/{record.id}/appeals",
        json={"requester_type": "vendor", "requester_id": "vendor-17", "reason": "Wrong amount"},
        headers=api.headers(org_a, "clerk"),
    )
    assert filed.status_code == 201, filed.text
    appeal = filed.json()
    assert appeal["status"] == "open"
    # Readers without decide or personal data permission see the reason redacted.
    queue = await api.client.get("/v1/appeals", headers=api.headers(org_a, "auditor"))
    assert [a["reason"] for a in queue.json()] == [{"redacted": True}]

    resolve = f"/v1/appeals/{appeal['id']}/resolve"
    body = {"outcome": "overturned", "outcome_text": "The amount was mis-read; overturned."}
    # Step-up, an outcome text, and segregation of duties.
    no_otp = await api.client.post(resolve, json=body, headers=api.headers(org_a, "fc", otp=False))
    assert no_otp.status_code == 403
    empty = await api.client.post(
        resolve, json={"outcome": "upheld", "outcome_text": ""}, headers=api.headers(org_a, "fc")
    )
    assert empty.status_code == 422
    filer = await api.client.post(resolve, json=body, headers=api.headers(org_a, "admin"))
    assert filer.status_code == 200  # admin did not file it
    resolved = filer.json()
    assert resolved["status"] == "resolved"
    assert resolved["outcome_text"] == body["outcome_text"]
    again = await api.client.post(resolve, json=body, headers=api.headers(org_a, "fc"))
    assert again.status_code == 409

    (outcome,) = await records(engine, org_a, kind="appeal_outcome")
    assert outcome.supersedes == record.id
    assert outcome.final_action == "appeal_overturned"
    assert resolved["outcome_decision_id"] == outcome.id
    actions = [
        r.action
        for r in await owner_sql(
            owner_engine, org_a.org_id, "SELECT action FROM audit_events ORDER BY seq"
        )
    ]
    assert {"core.appeal.created", "core.appeal.resolved"} <= set(actions)
    # A resolved appeal is frozen for the application role.
    async with session_for(engine, org_a.org_id) as s:
        with pytest.raises(DBAPIError):
            async with s.begin():
                await s.execute(text("UPDATE appeals SET outcome = 'upheld'"))


async def hire_record(engine: AsyncEngine, org: Org) -> str:
    """A hire screening decision (the Hire workflow ships in M2; recorded directly)."""
    from pairly_core.decisions.models import DecisionKind
    from pairly_core.decisions.service import DecisionDraft, record_decision

    async with session_for(engine, org.org_id) as s, s.begin():
        stored = await record_decision(
            s,
            DecisionDraft(
                kind=DecisionKind.SCORE,
                workflow="hire",
                entity_type="candidate",
                entity_id=f"cand-{uuid.uuid4().hex[:8]}",
                actor_type=ActorType.SYSTEM,
                actor_id="screening",
                output={"score": 3},
            ),
        )
    return stored.id


async def test_records_and_appeals_are_scoped_per_workflow(
    api: Api, engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    pay = await rules_record(engine, org_a)
    hire = await hire_record(engine, org_a)

    async def get(path: str, name: str, **kw: Any) -> httpx.Response:
        return await api.client.get(path, headers=api.headers(org_a, name, **kw))

    async def post(path: str, name: str, body: dict[str, Any], **kw: Any) -> httpx.Response:
        return await api.client.post(path, json=body, headers=api.headers(org_a, name, **kw))

    # Records and receipts: a pay-only approver sees pay, never hire (404, like another
    # organisation's record); a hire-only manager the other way round; the auditor both.
    assert (await get(f"/v1/decisions/{pay.id}/receipt", "pay_approver")).status_code == 200
    assert (await get(f"/v1/decisions/{hire}/receipt", "pay_approver")).status_code == 404
    assert (await get(f"/v1/decisions/{hire}/receipt.pdf", "pay_approver")).status_code == 404
    assert (await get(f"/v1/decisions/{hire}/replays", "pay_approver")).status_code == 404
    assert (await get(f"/v1/decisions/{hire}/receipt", "hire_manager")).status_code == 200
    assert (await get(f"/v1/decisions/{pay.id}/receipt", "hire_manager")).status_code == 404
    for name in ("auditor", "admin"):
        assert (await get(f"/v1/decisions/{hire}/receipt", name)).status_code == 200
    (hire_row,) = [r for r in await records(engine, org_a) if r.id == hire]
    listed = await get(
        f"/v1/decisions?entity_type=candidate&entity_id={hire_row.entity_id}", "pay_approver"
    )
    assert listed.status_code == 200
    assert listed.json() == []
    # Replay needs core.decision.replay on top of read access.
    assert (await post(f"/v1/decisions/{pay.id}/replay", "pay_approver", {})).status_code == 403

    # Appeals: the clerk (pay) cannot even see the hire record to file against it.
    reason = {"requester_type": "candidate", "requester_id": "cand-1", "reason": "Please review"}
    assert (await post(f"/v1/decisions/{hire}/appeals", "clerk", reason)).status_code == 404
    hire_appeal = await post(f"/v1/decisions/{hire}/appeals", "admin", reason)
    assert hire_appeal.status_code == 201, hire_appeal.text
    pay_body = {**reason, "requester_type": "vendor", "requester_id": "vendor-9"}
    pay_appeal = await post(f"/v1/decisions/{pay.id}/appeals", "clerk", pay_body)
    assert pay_appeal.status_code == 201, pay_appeal.text
    hire_id, pay_id = hire_appeal.json()["id"], pay_appeal.json()["id"]

    queue = (await get("/v1/appeals", "pay_approver")).json()
    assert [a["id"] for a in queue] == [pay_id]
    # A decider of the workflow reads the requester's own words.
    assert queue[0]["reason"] == "Please review"
    assert (await get(f"/v1/appeals/{hire_id}", "pay_approver")).status_code == 404
    assert [a["id"] for a in (await get("/v1/appeals", "hire_manager")).json()] == [hire_id]
    auditor = (await get("/v1/appeals", "auditor")).json()
    assert {a["id"] for a in auditor} == {hire_id, pay_id}
    assert all(a["reason"] == {"redacted": True} for a in auditor)
    # The clerk reads pay appeals but cannot decide them: the reason stays redacted.
    clerk_queue = (await get("/v1/appeals", "clerk")).json()
    assert [(a["id"], a["reason"]) for a in clerk_queue] == [(pay_id, {"redacted": True})]

    outcome = {"outcome": "upheld", "outcome_text": "Reviewed again; the decision stands."}
    # Outside the approver's workflows the appeal does not exist; inside, step-up applies.
    hidden = await post(f"/v1/appeals/{hire_id}/resolve", "pay_approver", outcome)
    assert hidden.status_code == 404
    no_otp = await post(f"/v1/appeals/{pay_id}/resolve", "pay_approver", outcome, otp=False)
    assert no_otp.status_code == 403
    clerk = await post(f"/v1/appeals/{pay_id}/resolve", "clerk", outcome)
    assert clerk.status_code == 403
    done = await post(f"/v1/appeals/{pay_id}/resolve", "pay_approver", outcome)
    assert done.status_code == 200, done.text
    hire_done = await post(f"/v1/appeals/{hire_id}/resolve", "hire_manager", outcome)
    assert hire_done.status_code == 200, hire_done.text

    # No workflow at all: an audited 403, never a silent empty list.
    assert (await get("/v1/appeals", "reader")).status_code == 403
    denials = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT payload->>'reason' AS reason FROM audit_events "
        "WHERE action = 'core.authz.denied' AND actor_id = :u",
        {"u": str(org_a.user("reader"))},
    )
    assert "no_workflow_access" in {d.reason for d in denials}


async def test_appeal_filer_cannot_resolve_and_sla_escalates(
    engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    from pairly_core.decisions.appeals import resolve_appeal
    from pairly_core.decisions.models import AppealOutcome
    from pairly_core.rbac import PermissionDeniedError

    record = await rules_record(engine, org_a)
    async with session_for(engine, org_a.org_id) as s, s.begin():
        late = await create_appeal(
            s,
            org_a.actor("fc"),
            decision_id=record.id,
            requester_type=RequesterType.VENDOR,
            requester_id="vendor-1",
            reason="Please look again",
            sla=timedelta(seconds=-60),
        )
    with pytest.raises(PermissionDeniedError):
        async with session_for(engine, org_a.org_id) as s, s.begin():
            await resolve_appeal(
                s, org_a.actor("fc"), late.id, outcome=AppealOutcome.UPHELD, outcome_text="ok"
            )
    for expected in (1, 0):
        async with session_for(engine, org_a.org_id) as s, s.begin():
            assert len(await escalate_overdue_appeals(s)) == expected
    rows = await owner_sql(
        owner_engine,
        org_a.org_id,
        "SELECT entity_id FROM audit_events WHERE action = 'core.appeal.escalated'",
    )
    assert [r.entity_id for r in rows] == [str(late.id)]
    # The worker finds the org through the SECURITY DEFINER listing (only overdue ones).
    from pairly_worker.decision_jobs import escalate_appeals

    assert (await escalate_appeals({"sessionmaker": create_sessionmaker(engine)}))["done"] == 0


# ------------------------------------------------------------------ compliance pack


async def test_compliance_pack_manifest_hashes(
    api: Api, engine: AsyncEngine, owner_engine: AsyncEngine, org_a: Org
) -> None:
    rules = await rules_record(engine, org_a)
    _, extraction = await stored_extraction(engine, org_a)
    await replay(engine, org_a, rules)
    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC) + timedelta(hours=1)
    body = {"from": start.isoformat(), "to": end.isoformat(), "workflows": ["pay"]}
    assert (
        await api.client.post(
            "/v1/compliance/export", json=body, headers=api.headers(org_a, "auditor", otp=False)
        )
    ).status_code == 403
    requested = await api.client.post(
        "/v1/compliance/export", json=body, headers=api.headers(org_a, "auditor")
    )
    assert requested.status_code == 202, requested.text
    export_id = requested.json()["id"]
    assert api.queue.jobs == [
        (COMPLIANCE_EXPORT_JOB, (str(org_a.org_id), export_id), f"compliance:{export_id}")
    ]
    early = await api.client.post(
        f"/v1/compliance/export/{export_id}/download", headers=api.headers(org_a, "auditor")
    )
    assert early.status_code == 409

    result = await run_export(
        create_sessionmaker(engine), api.store, org_a.org_id, uuid.UUID(export_id)
    )
    assert result["status"] == "ready", result
    status = (
        await api.client.get(
            f"/v1/compliance/export/{export_id}", headers=api.headers(org_a, "auditor")
        )
    ).json()
    assert status["status"] == "ready"
    data = api.store.objects[compliance_key(org_a.org_id, uuid.UUID(export_id))].data
    assert hashlib.sha256(data).hexdigest() == status["sha256"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert hashlib.sha256(manifest_bytes).hexdigest() == status["manifest_sha256"]
        for name, digest in manifest_files(manifest).items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        lines = archive.read("decision_records.jsonl").decode().splitlines()
        exported = [json.loads(line) for line in lines]
        policies = archive.read("policy_versions.jsonl").decode()
        replays = archive.read("replays.jsonl").decode()
        audit = json.loads(archive.read("audit_events.json"))
    # Only the pay workflow, no decrypted personal data.
    assert [r["decision_id"] for r in exported] == [rules.id]
    assert extraction.id not in json.dumps(exported)
    assert exported[0]["personal_data"]["facts"] == {"redacted": True}
    assert "pay.spend" in policies
    assert rules.id in replays
    assert audit["format"] == "pairly.audit.v1"
    assert manifest["audit_chain"]["ok"] is True
    assert {item["item"] for item in manifest["not_included"]} == {
        "fairness_reports",
        "model_cards",
    }
    link = await api.client.post(
        f"/v1/compliance/export/{export_id}/download", headers=api.headers(org_a, "auditor")
    )
    assert link.status_code == 200
    assert api.store.verify_url(link.json()["url"])
    actions = {
        r.action
        for r in await owner_sql(owner_engine, org_a.org_id, "SELECT action FROM audit_events")
    }
    assert {
        "core.compliance.export_requested",
        "core.compliance.export_ready",
        "core.compliance.export_downloaded",
    } <= actions
    # Running the job again does nothing.
    again = await run_export(
        create_sessionmaker(engine), api.store, org_a.org_id, uuid.UUID(export_id)
    )
    assert again == {"status": "skipped"}


async def test_export_without_storage_fails_closed(engine: AsyncEngine, org_a: Org) -> None:
    from pairly_core.decisions.compliance import request_export

    async with session_for(engine, org_a.org_id) as s, s.begin():
        row = await request_export(
            s,
            org_a.actor("auditor"),
            period_start=datetime.now(UTC) - timedelta(days=1),
            period_end=datetime.now(UTC),
        )
    result = await run_export(create_sessionmaker(engine), None, org_a.org_id, row.id)
    assert result == {"status": "failed", "error": "storage_unavailable"}


# ------------------------------------------------------------------ tenant isolation


async def test_new_tables_are_tenant_isolated(
    api: Api, engine: AsyncEngine, org_a: Org, org_b: Org
) -> None:
    record = await rules_record(engine, org_a)
    await replay(engine, org_a, record)
    async with session_for(engine, org_a.org_id) as s, s.begin():
        appeal = await create_appeal(
            s,
            org_a.actor("clerk"),
            decision_id=record.id,
            requester_type=RequesterType.CANDIDATE,
            requester_id="cand-1",
            reason="Why?",
        )
    requested = await api.client.post(
        "/v1/compliance/export",
        json={"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
        headers=api.headers(org_a, "admin"),
    )
    export_id = requested.json()["id"]

    tables = (
        "decision_records",
        "decision_replays",
        "appeals",
        "compliance_exports",
        "audit_verifications",
        "audit_denial_limits",
    )
    async with session_for(engine, org_b.org_id) as s, s.begin():
        for table in tables:
            count = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 0, table
    async with session_for(engine, org_b.org_id) as s:
        with pytest.raises(DBAPIError):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO appeals (org_id, decision_id, requester_type, requester_id, "
                        "reason, status, sla_due_at) VALUES "
                        "(:o, :d, 'vendor', 'x', 'v1:x', 'open', now())"
                    ),
                    {"o": org_a.org_id, "d": record.id},
                )

    b = org_b
    for method, path, body in (
        ("GET", f"/v1/decisions/{record.id}/receipt", None),
        ("GET", f"/v1/decisions/{record.id}/receipt.pdf", None),
        ("POST", f"/v1/decisions/{record.id}/replay", {}),
        ("GET", f"/v1/decisions/{record.id}/replays", None),
        (
            "POST",
            f"/v1/decisions/{record.id}/appeals",
            {"requester_type": "vendor", "requester_id": "v", "reason": "x"},
        ),
        ("GET", f"/v1/appeals/{appeal.id}", None),
        ("POST", f"/v1/appeals/{appeal.id}/resolve", {"outcome": "upheld", "outcome_text": "x"}),
        ("GET", f"/v1/compliance/export/{export_id}", None),
        ("POST", f"/v1/compliance/export/{export_id}/download", None),
    ):
        response = await api.client.request(
            method, path, json=body, headers=api.headers(b, "admin")
        )
        assert response.status_code == 404, (path, response.text)
    listed = await api.client.get(
        "/v1/decisions?entity_type=invoice&entity_id=inv-7", headers=api.headers(b, "admin")
    )
    assert listed.json() == []
    assert (await api.client.get("/v1/appeals", headers=api.headers(b, "admin"))).json() == []


async def test_approval_hooks_write_records(owner_engine: AsyncEngine, migrated: DbUrls) -> None:
    from pairly_core.approvals.hooks import ApprovalHooks
    from pairly_core.approvals.models import Decision
    from pairly_core.approvals.service import decide
    from pairly_core.decisions.hooks import DecisionRecordHook
    from pairly_core.workflow.checkpoint import TenantCheckpointer
    from pairly_core.workflow.runtime import WorkflowRuntime

    from .wf_support import EXECUTED, SPEC, seed_world, start
    from .wf_support import REGISTRY as WF_REGISTRY

    EXECUTED.clear()
    eng = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    try:
        runtime = WorkflowRuntime(
            sessionmaker=create_sessionmaker(eng),
            checkpointer=TenantCheckpointer(migrated.app),
            registry=WF_REGISTRY,
            graphs=[SPEC],
            hooks=ApprovalHooks([DecisionRecordHook()]),
        )
        world = await seed_world(owner_engine, "dec")
        run = await start(runtime, world, "120")
        async with session_for(eng, world.org_id) as s, s.begin():
            await decide(
                s,
                world.actor("appr1"),
                run.pending_approval_id,
                decision=Decision.APPROVE,
                reason="Looks right",
                idempotency_key="k-hooks",
                hooks=runtime.hooks,
            )
        await runtime.resume_run(world.org_id, run.id)
        async with session_for(eng, world.org_id) as s, s.begin():
            rows = list(await s.scalars(select(DecisionRecord).order_by(DecisionRecord.created_at)))
            decided_event = await s.scalar(
                select(AuditEvent.id).where(AuditEvent.action == "core.approval.decided")
            )
        assert [r.kind for r in rows] == ["approval", "final_action"], [
            (r.kind, r.output) for r in rows
        ]
        approval, final = rows
        assert (approval.kind, final.kind) == ("approval", "final_action")
        assert approval.final_action == "approve"
        assert approval.actor_id == str(world.id("appr1"))
        assert open_sensitive(approval)["reason"] == "Looks right"
        assert list(approval.approval_ids) == [run.pending_approval_id]
        assert list(approval.audit_event_ids) == [decided_event]
        assert approval.policy_versions[0]["kind"] == "approval_rules"
        assert final.supersedes == approval.id
        assert final.final_action == "executed:pay"
        assert final.output["approver_ids"] == [str(world.id("appr1"))]
    finally:
        await eng.dispose()

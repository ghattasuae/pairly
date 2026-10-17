# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Model router against a real Postgres: model_calls, RLS, budgets, recordings, replay.

Providers are scripted (``FakeProvider``); no request ever leaves the process, even if
provider keys happen to be set in the environment.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.errors import NotFoundError
from pairly_core.llm.budget import BUDGET_ALERT_ACTION, BudgetManager, set_org_budget
from pairly_core.llm.bundle import (
    BundleError,
    dumps_bundle,
    export_recordings,
    import_recordings,
    parse_bundle,
)
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.errors import BudgetExceeded, ModelUnavailable, RecordingMissing
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.resilience import RetryPolicy
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.store import DatabaseCallStore
from pairly_core.llm.types import TaskSpec

from .conftest import DbUrls, TwoTenants

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5-5"
TASK = TaskSpec(name="classify", prompt_version="v1", confidence_threshold=0.7)
INPUTS = {"text": "Invoice from ACME"}
GOOD = {"label": "invoice", "confidence": 0.95}
LLM_TABLES = ("model_calls", "llm_budget_reservations", "org_llm_budgets", "llm_budget_alerts")


class Answer(BaseModel):
    label: str = Field(max_length=40)
    confidence: float = Field(ge=0, le=1)


@pytest.fixture
def prompts(tmp_path: Path) -> PromptRegistry:
    (tmp_path / "classify").mkdir()
    (tmp_path / "classify" / "v1.md").write_text(
        "---\ntask: classify\nversion: v1\ntier: small\ntemperature: 0\n"
        "output_schema: Answer\nmax_output_tokens: 512\n---\nClassify the input.\n"
    )
    return PromptRegistry([tmp_path])


@pytest.fixture
async def llm_engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated.app, pool_size=10, max_overflow=10)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_llm(owner_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[TwoTenants]:
    """Remove this module's rows before the tenants fixture drops the organisations."""
    yield tenants
    for tenant in (tenants.a, tenants.b):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(tenant.org_id)}
            )
            for table in LLM_TABLES:
                await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608  # nosec B608


def make_router(
    engine: AsyncEngine,
    prompts: PromptRegistry,
    *,
    mode: ModelMode = ModelMode.RECORD,
    anthropic: FakeProvider | None = None,
    default_limit_micros: int = 10_000_000,
) -> tuple[ModelRouter, FakeProvider]:
    prices = load_prices()
    provider = anthropic or FakeProvider("anthropic")
    router = ModelRouter(
        settings=LLMSettings(model_mode=mode),
        providers={"anthropic": provider, "openai": FakeProvider("openai", available=False)},
        catalog=load_catalog(prices),
        prices=prices,
        prompts=prompts,
        sessionmaker=create_sessionmaker(engine),
        budget=BudgetManager(default_limit_micros=default_limit_micros),
        store=DatabaseCallStore(),
        retry_policy=RetryPolicy(max_retries=0),
    )
    return router, provider


def _session(engine: AsyncEngine, org: uuid.UUID) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, None)
    return session


async def _rows(engine: AsyncEngine, org: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with _session(engine, org) as s, s.begin():
        return list((await s.execute(text(sql), params)).all())


# ------------------------------------------------------------------ recording modes


async def test_record_mode_then_recorded_hit_and_miss(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, {"label": "x" * 100, "confidence": 0.9})
    provider.script(SONNET, GOOD)
    live = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
    assert live.tier == "large"

    rows = await _rows(
        llm_engine,
        org,
        "SELECT id, mode, status, model, request_hash, cost_micros, response, prompt_hash, "
        "request FROM model_calls ORDER BY attempt",
    )
    assert [(r.mode, r.status, r.model) for r in rows] == [
        ("record", "invalid_output", HAIKU),
        ("record", "ok", SONNET),
    ]
    # Request and response are encrypted at rest (WP1.14); the store decrypts them.
    assert rows[1].request.startswith("v1:")
    assert rows[1].response.startswith("v1:")
    async with _session(llm_engine, org) as s, s.begin():
        stored = await DatabaseCallStore().get(s, rows[1].id)
    assert stored is not None
    assert stored.response == GOOD
    assert rows[1].request_hash == live.request_hash
    assert stored.request["inputs"] == INPUTS
    assert rows[1].cost_micros == live.cost_micros > 0

    recorded, offline = make_router(llm_engine, prompts, mode=ModelMode.RECORDED)
    hit = await recorded.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
    assert hit.value == live.value
    assert hit.from_recording
    assert offline.requests == []
    with pytest.raises(RecordingMissing):
        await recorded.complete(task=TASK, inputs={"text": "other"}, schema=Answer, org_id=org)
    assert offline.requests == []


async def test_a_recording_that_does_not_decrypt_fails_closed(
    llm_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    prompts: PromptRegistry,
    clean_llm: TwoTenants,
) -> None:
    org = clean_llm.a.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD)
    await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
    provider.script(HAIKU, {"label": "other", "confidence": 0.9})
    await router.complete(task=TASK, inputs={"text": "other"}, schema=Answer, org_id=org)
    rows = await _rows(llm_engine, org, "SELECT id, response FROM model_calls ORDER BY created_at")
    # Someone with database access moves another row's ciphertext into the recording.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        await conn.execute(
            text("UPDATE model_calls SET response = :r WHERE id = :id"),
            {"r": rows[1].response, "id": rows[0].id},
        )
    recorded, offline = make_router(llm_engine, prompts, mode=ModelMode.RECORDED)
    with pytest.raises(ModelUnavailable, match="could not be decrypted"):
        await recorded.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
    assert offline.requests == []


# ------------------------------------------------------------------ tenant isolation


async def test_model_calls_are_tenant_isolated(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    a, b = clean_llm.a.org_id, clean_llm.b.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD)
    result = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)

    # Org B sees none of A's calls, cannot serve A's recording, cannot replay A's call.
    assert await _rows(llm_engine, b, "SELECT id FROM model_calls") == []
    assert (
        await _rows(llm_engine, b, "SELECT id FROM model_calls WHERE id = :id", id=result.call_id)
        == []
    )
    recorded, _ = make_router(llm_engine, prompts, mode=ModelMode.RECORDED)
    with pytest.raises(RecordingMissing):
        await recorded.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=b)
    with pytest.raises(NotFoundError):
        await recorded.replay(result.call_id, org_id=b, schema=Answer)

    # B cannot write a row for A.
    with pytest.raises(DBAPIError):
        async with _session(llm_engine, b) as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO model_calls (org_id, chain_id, attempt, task, prompt_version, "
                    "prompt_hash, provider, model, tier, schema_name, schema_hash, request_hash, "
                    "mode, status, request) VALUES (:org, gen_random_uuid(), 1, 't', 'v1', "
                    "repeat('a', 64), 'p', 'm', 'small', 'S', repeat('a', 64), repeat('a', 64), "
                    "'live', 'ok', 'v1:x')"
                ),
                {"org": a},
            )

    # Append-only: the app role cannot change or delete a call record, even its own.
    for sql in ("UPDATE model_calls SET cost_micros = 0", "DELETE FROM model_calls"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with _session(llm_engine, a) as s, s.begin():
                await s.execute(text(sql))


async def test_llm_tables_force_row_level_security(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": list(LLM_TABLES)},
            )
        ).all()
    assert {r.relname for r in rows} == set(LLM_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows)


# ------------------------------------------------------------------ budget


async def _insert_spend(session: AsyncSession, org: uuid.UUID, cost: int) -> None:
    await DatabaseCallStore().insert(
        session,
        {
            "org_id": org,
            "chain_id": uuid.uuid4(),
            "attempt": 1,
            "task": "t",
            "prompt_version": "v1",
            "prompt_hash": "a" * 64,
            "provider": "fake",
            "model": "m",
            "tier": "small",
            "schema_name": "S",
            "schema_hash": "b" * 64,
            "request_hash": "c" * 64,
            "mode": "live",
            "status": "ok",
            "request": {"inputs": {}, "settings": {}},
            "cost_micros": cost,
        },
    )


async def _spend(
    engine: AsyncEngine, budget: BudgetManager, org: uuid.UUID, reserve: int, cost: int
) -> None:
    async with _session(engine, org) as s:
        rid = await budget.reserve(s, org, reserve)
    async with _session(engine, org) as s, s.begin():
        await _insert_spend(s, org, cost)
        await budget.settle(s, org, rid, cost)


async def test_reservations_are_atomic_under_concurrency(
    llm_engine: AsyncEngine, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    budget = BudgetManager(default_limit_micros=1000)

    async def try_reserve() -> bool:
        async with _session(llm_engine, org) as s:
            try:
                await budget.reserve(s, org, 100)
            except BudgetExceeded:
                return False
            return True

    results = await asyncio.gather(*(try_reserve() for _ in range(25)))
    # Exactly limit / worst_case calls are admitted; nobody can overbook.
    assert sum(results) == 10
    async with _session(llm_engine, org) as s, s.begin():
        status = await budget.status(s, org)
    assert status.reserved_micros == 1000
    assert status.spent_micros == 0


async def test_hard_stop_and_alerts_fire_once_under_concurrency(
    llm_engine: AsyncEngine, owner_engine: AsyncEngine, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    budget = BudgetManager(default_limit_micros=1_000_000)
    async with _session(llm_engine, org) as s, s.begin():
        await set_org_budget(s, org, 1000)  # the per-org override wins over the default
        await set_org_budget(s, org, 1000)  # upsert
    await _spend(llm_engine, budget, org, 300, 300)
    assert await _rows(llm_engine, org, "SELECT 1 FROM llm_budget_alerts") == []

    # Seven concurrent calls of 100: spend goes 300 -> 1000 and crosses 80% and 100%.
    await asyncio.gather(*(_spend(llm_engine, budget, org, 100, 100) for _ in range(7)))
    alerts = await _rows(
        llm_engine, org, "SELECT threshold_pct, limit_micros FROM llm_budget_alerts ORDER BY 1"
    )
    assert [(r.threshold_pct, r.limit_micros) for r in alerts] == [(80, 1000), (100, 1000)]

    # Hard stop: nothing more is admitted, and the 100% alert is not repeated.
    async with _session(llm_engine, org) as s:
        with pytest.raises(BudgetExceeded):
            await budget.reserve(s, org, 1)
    async with _session(llm_engine, org) as s, s.begin():
        status = await budget.status(s, org)
    assert (status.spent_micros, status.reserved_micros, status.limit_micros) == (1000, 0, 1000)
    events = await _rows(
        llm_engine,
        org,
        "SELECT payload FROM audit_events WHERE action = :a ORDER BY seq",
        a=BUDGET_ALERT_ACTION,
    )
    assert [e.payload["threshold_pct"] for e in events] == [80, 100]
    assert events[0].payload["limit_micros"] == 1000


async def test_refusal_at_full_budget_records_the_100_alert(
    llm_engine: AsyncEngine, clean_llm: TwoTenants
) -> None:
    org = clean_llm.b.org_id
    budget = BudgetManager(default_limit_micros=0)
    for _ in range(2):
        async with _session(llm_engine, org) as s:
            with pytest.raises(BudgetExceeded):
                await budget.reserve(s, org, 1)
    alerts = await _rows(llm_engine, org, "SELECT threshold_pct FROM llm_budget_alerts")
    assert [r.threshold_pct for r in alerts] == [100]


async def test_expired_reservations_stop_counting(
    llm_engine: AsyncEngine, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    budget = BudgetManager(default_limit_micros=100, reservation_ttl_seconds=60)
    async with _session(llm_engine, org) as s:
        await budget.reserve(s, org, 100)
    async with _session(llm_engine, org) as s:
        with pytest.raises(BudgetExceeded):
            await budget.reserve(s, org, 1)
    # Simulate a crashed caller whose reservation has expired.
    async with _session(llm_engine, org) as s, s.begin():
        await s.execute(text("DELETE FROM llm_budget_reservations"))
        await s.execute(
            text(
                "INSERT INTO llm_budget_reservations (org_id, amount_micros, expires_at) "
                "VALUES (:org, 100, now() - interval '1 second')"
            ),
            {"org": org},
        )
    async with _session(llm_engine, org) as s:
        await budget.reserve(s, org, 100)


async def test_router_hard_stop_sends_nothing(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    router, provider = make_router(llm_engine, prompts, default_limit_micros=0)
    provider.script(HAIKU, GOOD)
    with pytest.raises(BudgetExceeded):
        await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
    assert provider.requests == []
    assert await _rows(llm_engine, org, "SELECT 1 FROM model_calls") == []


async def test_router_spend_never_exceeds_limit_under_concurrency(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    router, _ = make_router(llm_engine, prompts)

    class SlowProvider(FakeProvider):
        async def generate(self, request: Any) -> Any:
            await asyncio.sleep(0.02)
            return await super().generate(request)

    slow = SlowProvider("anthropic")
    slow.script(HAIKU, GOOD)
    router._providers["anthropic"] = slow
    # Room for three worst-case Haiku calls at once.
    probe = router._provider_request(
        prompts.get("classify", "v1"),
        router_candidate(router),
        "x",
        Answer,
    )
    worst = router._worst_case_micros(probe)
    limit = 3 * worst + worst // 2
    async with _session(llm_engine, org) as s, s.begin():
        await set_org_budget(s, org, limit)

    async def call() -> bool:
        try:
            await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)
        except BudgetExceeded:
            return False
        return True

    results = await asyncio.gather(*(call() for _ in range(12)))
    assert 3 <= sum(results) < 12
    spent = await _rows(
        llm_engine, org, "SELECT coalesce(sum(cost_micros), 0) AS s FROM model_calls"
    )
    assert spent[0].s <= limit
    leftover = await _rows(llm_engine, org, "SELECT 1 FROM llm_budget_reservations")
    assert leftover == []


def router_candidate(router: ModelRouter) -> Any:
    from pairly_core.llm.router import _Candidate

    entry = router._catalog.model_for("anthropic", "small")
    assert entry is not None
    return _Candidate("small", "anthropic", entry, {"temperature": 0.0}, "0" * 64)


# ------------------------------------------------------------------ replay and bundles


async def test_replay_against_the_database(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    org = clean_llm.a.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD, {"label": "receipt", "confidence": 0.95})
    result = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=org)

    same = await router.replay(result.call_id, org_id=org, schema=Answer, task=TASK)
    assert same.matches
    assert same.diff == ()
    changed = await router.replay(result.call_id, org_id=org, schema=Answer, task=TASK, live=True)
    assert not changed.matches
    assert [d.path for d in changed.diff] == ["$.label"]
    replays = await _rows(
        llm_engine,
        org,
        "SELECT mode FROM model_calls WHERE replay_of = :id ORDER BY created_at",
        id=result.call_id,
    )
    assert sorted(r.mode for r in replays) == ["record", "recorded"]


async def test_reuse_never_crosses_orgs_or_reuses_a_replay(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    """Security review 2026-10 (L-01): response reuse in live mode, on the real table."""
    a, b = clean_llm.a.org_id, clean_llm.b.org_id
    router, provider = make_router(llm_engine, prompts, mode=ModelMode.LIVE)
    provider.script(HAIKU, GOOD, GOOD, {"label": "receipt", "confidence": 0.95})
    first = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)
    again = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)
    assert len(provider.requests) == 1
    assert again.value == first.value

    # Organisation B sends the byte-identical request: it never gets A's answer.
    other = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=b)
    assert len(provider.requests) == 2
    assert other.request_hash == first.request_hash
    rows_b = await _rows(llm_engine, b, "SELECT reused_from FROM model_calls")
    assert [r.reused_from for r in rows_b] == [None]

    # A live replay of A's call is a check, not an answer: once the original is out of
    # the window, the replay (a different answer) must not become the reused one.
    replayed = await router.replay(first.call_id, org_id=a, schema=Answer, task=TASK, live=True)
    assert not replayed.matches
    rows = await _rows(
        llm_engine,
        a,
        "SELECT id, created_at, replay_of FROM model_calls WHERE reused_from IS NULL "
        "ORDER BY created_at",
    )
    original, replay = rows
    assert replay.replay_of == original.id
    async with _session(llm_engine, a) as s, s.begin():
        found = await DatabaseCallStore().find_reusable(
            s, first.request_hash, org_id=a, since=original.created_at
        )
        later = await DatabaseCallStore().find_reusable(
            s, first.request_hash, org_id=a, since=replay.created_at
        )
        cross = await DatabaseCallStore().find_reusable(
            s, first.request_hash, org_id=b, since=original.created_at
        )
    assert found is not None
    assert found.id == original.id
    assert later is None
    assert cross is None


async def test_recorded_mode_never_serves_a_decision_replay(
    llm_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    prompts: PromptRegistry,
    clean_llm: TwoTenants,
) -> None:
    """Security review 2026-10 (L-06): a live replay of a recorded call stores a newer
    row with the same request hash and another answer. Recorded mode still serves the
    provider's answer, never the replay, on the real table."""
    a = clean_llm.a.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD, {"label": "receipt", "confidence": 0.95})
    first = await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)
    replayed = await router.replay(first.call_id, org_id=a, schema=Answer, task=TASK, live=True)
    assert not replayed.matches
    rows = await _rows(
        llm_engine, a, "SELECT id, replay_of FROM model_calls ORDER BY created_at, id"
    )
    assert [r.replay_of for r in rows] == [None, first.call_id]

    async with _session(llm_engine, a) as s, s.begin():
        found = await DatabaseCallStore().find_recording(s, first.request_hash)
    assert found is not None
    assert found.id == first.call_id
    recorded, offline = make_router(llm_engine, prompts, mode=ModelMode.RECORDED)
    hit = await recorded.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)
    assert hit.value == Answer.model_validate(GOOD)
    assert offline.requests == []

    # A hash whose only row is a replay has no recording at all.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(a)})
        await conn.execute(
            text("UPDATE model_calls SET request_hash = :h WHERE replay_of IS NOT NULL"),
            {"h": "f" * 64},
        )
    async with _session(llm_engine, a) as s, s.begin():
        assert await DatabaseCallStore().find_recording(s, "f" * 64) is None


async def test_bundle_round_trip_between_orgs(
    llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    a, b = clean_llm.a.org_id, clean_llm.b.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD)
    await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)

    async with _session(llm_engine, a) as s, s.begin():
        bundle = await export_recordings(s)
    text_bundle = dumps_bundle(bundle)
    entries = parse_bundle(text_bundle)
    assert len(entries) == 1

    async with _session(llm_engine, b) as s, s.begin():
        report = await import_recordings(s, b, entries)
    assert (report.imported, report.skipped) == (1, 0)
    async with _session(llm_engine, b) as s, s.begin():
        again = await import_recordings(s, b, entries)
    assert (again.imported, again.skipped) == (0, 1)

    recorded, offline = make_router(llm_engine, prompts, mode=ModelMode.RECORDED)
    hit = await recorded.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=b)
    assert hit.value == Answer.model_validate(GOOD)
    assert offline.requests == []
    # Imported rows cost nothing in the importing org.
    spent = await _rows(llm_engine, b, "SELECT coalesce(sum(cost_micros), 0) AS s FROM model_calls")
    assert spent[0].s == 0

    tampered = text_bundle.replace("Invoice from ACME", "Invoice from EVIL")
    with pytest.raises(BundleError, match="request_hash"):
        parse_bundle(tampered)


async def test_admin_cli_bundle_helpers(
    migrated: DbUrls, llm_engine: AsyncEngine, prompts: PromptRegistry, clean_llm: TwoTenants
) -> None:
    from pairly_api.admin import export_bundle, import_bundle

    a, b = clean_llm.a.org_id, clean_llm.b.org_id
    router, provider = make_router(llm_engine, prompts)
    provider.script(HAIKU, GOOD)
    await router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=a)
    exported = await export_bundle(migrated.owner, a, "classify")
    report = await import_bundle(migrated.owner, b, exported)
    assert report.imported == 1
    assert await export_bundle(migrated.owner, a, "other-task") != exported


def test_bundle_validation() -> None:
    with pytest.raises(BundleError, match="valid JSON"):
        parse_bundle("{")
    with pytest.raises(BundleError, match="not a Pairly"):
        parse_bundle('{"format": "x"}')
    with pytest.raises(BundleError, match="version"):
        parse_bundle('{"format": "pairly.model-recordings", "version": 9}')
    with pytest.raises(BundleError, match="entries"):
        parse_bundle('{"format": "pairly.model-recordings", "version": 1, "entries": {}}')
    with pytest.raises(BundleError, match="wrong fields"):
        parse_bundle('{"format": "pairly.model-recordings", "version": 1, "entries": [{}]}')

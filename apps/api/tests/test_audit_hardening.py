# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Audit hardening (WP1.10b) on real Postgres: anchors, truncation detection, the
safety net, denial rate limits and worker-side verification of large chains."""

from __future__ import annotations

import base64
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api.decision_services import DecisionServices
from pairly_api.main import create_app
from pairly_core.audit import ActorType, AuditAction, append_audit, verify_chain
from pairly_core.audit_anchor.anchoring import anchor_map, anchor_org, load_anchors, public_keys
from pairly_core.audit_anchor.denials import flush_suppressed, record_denial_limited
from pairly_core.audit_anchor.signing import AnchorError, encode_key, key_id
from pairly_core.audit_anchor.store import InMemoryAnchorStore, anchor_key
from pairly_core.audit_anchor.verification import run_verification
from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker
from pairly_core.decisions.jobs import VERIFY_AUDIT_CHAIN_JOB, RecordingTaskQueue
from pairly_core.identity import OrgSession
from pairly_core.models import Organisation
from pairly_core.rbac import Actor, Denial, require
from pairly_worker.decision_jobs import anchor_audit_chains, flush_denials, sweep_decision_jobs

from .conftest import DbUrls, TokenFactory, make_settings
from .test_decisions import PLUGINS, Org, owner_sql, seed_org, session_for


async def append(engine: AsyncEngine, org: uuid.UUID, n: int) -> None:
    async with session_for(engine, org) as s, s.begin():
        for i in range(n):
            await append_audit(
                s,
                action="core.test.done",
                actor_type=ActorType.SYSTEM,
                actor_id="t",
                payload={"i": i},
            )


async def truncate_tail(owner: AsyncEngine, org: uuid.UUID, keep: int) -> None:
    """What an attacker with the owner role could do: drop the newest events."""
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable"))
        await conn.execute(
            text("DELETE FROM audit_events WHERE org_id = :o AND seq > :k"), {"o": org, "k": keep}
        )
        await conn.execute(
            text("ALTER TABLE audit_events ENABLE ALWAYS TRIGGER audit_events_immutable")
        )


@pytest.fixture
async def engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    yield eng
    await eng.dispose()


@pytest.fixture
async def org(owner_engine: AsyncEngine) -> Org:
    return await seed_org(owner_engine, "h")


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


# ------------------------------------------------------------------ anchors


async def test_anchor_signing_and_truncation_detection(
    engine: AsyncEngine, owner_engine: AsyncEngine, org: Org, key: Ed25519PrivateKey
) -> None:
    store = InMemoryAnchorStore()
    keys = public_keys(key.public_key())
    await append(engine, org.org_id, 5)
    async with session_for(engine, org.org_id) as s, s.begin():
        first = await anchor_org(s, store, key)
        again = await anchor_org(s, store, key)
    assert (first.status, first.seq, again.status) == ("written", 5, "unchanged")
    await append(engine, org.org_id, 2)
    async with session_for(engine, org.org_id) as s, s.begin():
        assert (await anchor_org(s, store, key)).seq == 7
    heads = await load_anchors(store, org.org_id, keys)
    assert [h.seq for h in heads] == [5, 7]
    anchors = anchor_map(heads)
    async with session_for(engine, org.org_id) as s, s.begin():
        assert (await verify_chain(s, org.org_id, anchors=anchors)).ok

    # Tail truncation: still a valid chain on its own, caught by the anchors.
    await truncate_tail(owner_engine, org.org_id, keep=6)
    async with session_for(engine, org.org_id) as s, s.begin():
        assert (await verify_chain(s, org.org_id)).ok
        result = await verify_chain(s, org.org_id, anchors=anchors)
    assert (result.ok, result.reason, result.broken_seq) == (False, "anchor_truncated", 7)

    # Re-growing the chain to the same length gives a different head: a conflict.
    await append(engine, org.org_id, 1)
    async with session_for(engine, org.org_id) as s, s.begin():
        assert (await anchor_org(s, store, key)).status == "conflict"
        mismatch = await verify_chain(s, org.org_id, anchors=anchors)
    assert (mismatch.ok, mismatch.reason, mismatch.broken_seq) == (False, "anchor_mismatch", 7)


async def test_tampered_or_foreign_anchors_fail_closed(
    engine: AsyncEngine, org: Org, key: Ed25519PrivateKey
) -> None:
    store = InMemoryAnchorStore()
    await append(engine, org.org_id, 2)
    async with session_for(engine, org.org_id) as s, s.begin():
        await anchor_org(s, store, key)
    object_key = anchor_key(org.org_id, 2)
    doc = json.loads(store.objects[object_key])
    doc["hash"] = "0" * 64
    store.objects[object_key] = json.dumps(doc).encode()
    with pytest.raises(AnchorError, match="signature"):
        await load_anchors(store, org.org_id, public_keys(key.public_key()))
    other = Ed25519PrivateKey.generate()
    with pytest.raises(AnchorError, match="unknown key"):
        await load_anchors(store, org.org_id, public_keys(other.public_key()))
    # Write once: the store refuses to replace an anchor.
    assert not await store.put_new(object_key, b"{}")


async def test_nightly_anchor_job(engine: AsyncEngine, org: Org, key: Ed25519PrivateKey) -> None:
    store = InMemoryAnchorStore()
    await append(engine, org.org_id, 3)
    ctx: dict[str, Any] = {
        "sessionmaker": create_sessionmaker(engine),
        "anchor_store": store,
        "audit_signing_key": key,
    }
    summary = await anchor_audit_chains(ctx)
    assert summary["written"] >= 1
    assert anchor_key(org.org_id, 3) in store.objects
    assert summary["conflicts"] == []
    assert (await anchor_audit_chains(ctx | {"anchor_store": None})) == {"skipped": True}


# ------------------------------------------------------------------ API


@dataclasses.dataclass
class Live:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    queue: RecordingTaskQueue
    store: InMemoryAnchorStore

    def headers(self, org: Org, name: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token(org.people[name][1], amr=['otp'])}",
            "X-Pairly-Org": str(org.org_id),
        }


def _app(migrated: DbUrls, services: DecisionServices, env: Environment) -> FastAPI:
    settings = make_settings(migrated.app).model_copy(update={"env": env})
    app = create_app(settings, plugins=PLUGINS, decision_services=services)

    @app.post("/v1/test/unaudited")
    async def unaudited(
        actor: Annotated[Actor, Depends(require("core.org.manage"))], session: OrgSession
    ) -> dict[str, str]:
        row = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
        assert row is not None
        row.name = row.name + " (renamed)"
        return {"ok": "yes"}

    return app


@pytest.fixture
async def live(
    migrated: DbUrls, tokens: TokenFactory, key: Ed25519PrivateKey
) -> AsyncIterator[Live]:
    queue = RecordingTaskQueue()
    store = InMemoryAnchorStore()
    services = DecisionServices(
        queue=queue, anchor_public_key=key.public_key(), anchor_store=store, large_chain_events=10
    )
    app = _app(migrated, services, Environment.TEST)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Live(app, client, tokens, queue, store)


async def test_anchor_key_and_verify_api(
    live: Live,
    engine: AsyncEngine,
    owner_engine: AsyncEngine,
    org: Org,
    key: Ed25519PrivateKey,
) -> None:
    published = (await live.client.get("/v1/audit/anchor-key")).json()
    assert published["algorithm"] == "Ed25519"
    assert published["key_id"] == key_id(key.public_key())
    raw = base64.urlsafe_b64decode(published["public_key"])
    assert encode_key(raw) == published["public_key"]

    await append(engine, org.org_id, 4)
    async with session_for(engine, org.org_id) as s, s.begin():
        await anchor_org(s, live.store, key)
    result = (
        await live.client.get("/v1/audit/verify", headers=live.headers(org, "auditor"))
    ).json()
    assert (result["ok"], result["anchors_checked"], result["anchored_seq"]) == (True, True, 4)
    listed = (
        await live.client.get("/v1/audit/anchors", headers=live.headers(org, "auditor"))
    ).json()
    assert [a["seq"] for a in listed] == [4]

    await truncate_tail(owner_engine, org.org_id, keep=3)
    broken = (
        await live.client.get("/v1/audit/verify", headers=live.headers(org, "auditor"))
    ).json()
    assert (broken["ok"], broken["reason"]) == (False, "anchor_truncated")


async def test_large_chain_is_verified_by_the_worker(
    live: Live, engine: AsyncEngine, org: Org, key: Ed25519PrivateKey
) -> None:
    await append(engine, org.org_id, 12)
    response = await live.client.get("/v1/audit/verify", headers=live.headers(org, "auditor"))
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert live.queue.jobs == [
        (VERIFY_AUDIT_CHAIN_JOB, (str(org.org_id), job_id), f"verify:{job_id}")
    ]
    pending = await live.client.get(
        f"/v1/audit/verify/{job_id}", headers=live.headers(org, "auditor")
    )
    assert pending.json()["status"] == "pending"
    outcome = await run_verification(
        create_sessionmaker(engine), org.org_id, uuid.UUID(job_id), anchors=None
    )
    assert outcome == {"status": "done", "ok": True}
    done = (
        await live.client.get(f"/v1/audit/verify/{job_id}", headers=live.headers(org, "auditor"))
    ).json()
    assert done["status"] == "done"
    assert done["result"]["ok"] is True
    assert done["result"]["last_seq"] >= 12
    # A finished job is never run twice; the sweeper finds nothing stale.
    again = await run_verification(create_sessionmaker(engine), org.org_id, uuid.UUID(job_id))
    assert again == {"status": "skipped"}
    sweep = await sweep_decision_jobs({"sessionmaker": create_sessionmaker(engine)})
    assert sweep["errors"] == []
    other = await live.client.get(
        f"/v1/audit/verify/{uuid.uuid4()}", headers=live.headers(org, "auditor")
    )
    assert other.status_code == 404


# ------------------------------------------------------------------ safety net


async def test_safety_net_raises_in_test_and_warns_in_production(
    live: Live, owner_engine: AsyncEngine, org: Org
) -> None:
    response = await live.client.post("/v1/test/unaudited", headers=live.headers(org, "admin"))
    assert response.status_code == 500
    assert response.json()["code"] == "unaudited_write"
    (name,) = await owner_sql(
        owner_engine, org.org_id, "SELECT name FROM organisations WHERE id = :o", {"o": org.org_id}
    )
    assert "(renamed)" not in name.name  # rolled back

    # Production: the same write gets the generic event and a warning instead.
    context = live.app.state.pairly
    production = context.settings.model_copy(update={"env": Environment.PRODUCTION})
    live.app.state.pairly = dataclasses.replace(context, settings=production)
    try:
        ok = await live.client.post("/v1/test/unaudited", headers=live.headers(org, "admin"))
    finally:
        live.app.state.pairly = context
    assert ok.status_code == 200
    events = await owner_sql(
        owner_engine,
        org.org_id,
        "SELECT payload FROM audit_events WHERE action = :a",
        {"a": AuditAction.REQUEST_WRITE},
    )
    assert [e.payload["route"] for e in events] == ["/v1/test/unaudited"]


# ------------------------------------------------------------------ denial rate limit


async def test_denials_are_rate_limited_and_summarised(
    engine: AsyncEngine, owner_engine: AsyncEngine, org: Org
) -> None:
    actor = org.actor("reader")
    denial = Denial(actor=actor, permission="core.audit.export", reason="missing_permission")
    start = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    written = []
    for i in range(10):
        async with session_for(engine, org.org_id) as s, s.begin():
            written.append(
                await record_denial_limited(
                    s, denial, capacity=3, refill_per_second=0.1, now=start + timedelta(seconds=i)
                )
            )
    # Three from the full bucket, then one refilled token at t=9 (0.9 + 0.1 * ...).
    assert written[:3] == [True, True, True]
    assert written.count(True) < 10
    suppressed = written.count(False)

    async def actions() -> list[str]:
        rows = await owner_sql(
            owner_engine, org.org_id, "SELECT action, payload FROM audit_events ORDER BY seq"
        )
        return [r.action for r in rows]

    assert (await actions()).count(AuditAction.AUTHZ_DENIED) == written.count(True)
    # Much later: the next denial first writes one summary of what was held back.
    async with session_for(engine, org.org_id) as s, s.begin():
        assert await record_denial_limited(
            s, denial, capacity=3, refill_per_second=0.1, now=start + timedelta(minutes=5)
        )
    rows = await owner_sql(
        owner_engine,
        org.org_id,
        "SELECT payload FROM audit_events WHERE action = :a",
        {"a": AuditAction.AUTHZ_DENIALS_SUPPRESSED},
    )
    assert [r.payload["count"] for r in rows] == [suppressed]
    assert rows[0].payload["permissions"] == ["core.audit.export"]

    # A burst followed by silence is summarised by the flush job, not lost.
    for _ in range(5):
        async with session_for(engine, org.org_id) as s, s.begin():
            await record_denial_limited(
                s, denial, capacity=3, refill_per_second=0.0, now=start + timedelta(minutes=6)
            )
    async with session_for(engine, org.org_id) as s, s.begin():
        assert await flush_suppressed(s, now=start + timedelta(minutes=6, seconds=30)) == 0
    summary = await flush_denials({"sessionmaker": create_sessionmaker(engine)})
    assert summary["errors"] == []
    rows = await owner_sql(
        owner_engine,
        org.org_id,
        "SELECT payload FROM audit_events WHERE action = :a ORDER BY seq",
        {"a": AuditAction.AUTHZ_DENIALS_SUPPRESSED},
    )
    assert len(rows) == 2
    assert rows[1].payload["count"] >= 3
    async with session_for(engine, org.org_id) as s, s.begin():
        assert (await verify_chain(s, org.org_id)).ok

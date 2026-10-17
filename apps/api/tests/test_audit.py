# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Audit log against a real Postgres: chain, immutability, isolation, concurrency, API.

Tamper tests edit a row as the table owner, disabling the immutability trigger inside
one transaction (DDL is transactional, so no other session ever sees it disabled), and
put the original value back afterwards so the test database keeps valid chains.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from pairly_api.main import create_app
from pairly_core.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    AuditRecord,
    append_audit,
    audit_actor_action,
    record_membership_change,
    verify_chain,
    verify_records,
)
from pairly_core.db import TenantError, create_sessionmaker, set_tenant
from pairly_core.identity import OrgSession
from pairly_core.models import Organisation
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import Actor, ActorKind, RoleGrant, build_registry, require
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, TwoTenants, make_settings

PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin]
IMMUTABLE_TRIGGER = "audit_events_immutable"


def _session(
    engine: AsyncEngine, org: uuid.UUID | None, subject: str | None = None
) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, subject)
    return session


async def _append(
    engine: AsyncEngine, org: uuid.UUID, n: int, action: str = "core.test.done"
) -> None:
    async with _session(engine, org) as s, s.begin():
        for i in range(n):
            await append_audit(
                s,
                action=action,
                actor_type=ActorType.SYSTEM,
                actor_id="tests",
                entity_type="thing",
                entity_id=f"t-{i}",
                payload={"i": i},
            )


async def _verify(engine: AsyncEngine, org: uuid.UUID) -> Any:
    async with _session(engine, org) as s, s.begin():
        return await verify_chain(s, org)


async def _owner_tx(engine: AsyncEngine, org: uuid.UUID) -> Any:
    conn = await engine.connect()
    await conn.begin()
    await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
    return conn


async def _tamper(owner: AsyncEngine, org: uuid.UUID, seq: int, payload: dict[str, Any]) -> None:
    """Edit one event's payload behind the trigger's back (test-only owner connection)."""
    conn = await _owner_tx(owner, org)
    try:
        await conn.execute(text(f"ALTER TABLE audit_events DISABLE TRIGGER {IMMUTABLE_TRIGGER}"))
        result = await conn.execute(
            text(
                "UPDATE audit_events SET payload = CAST(:p AS jsonb) WHERE org_id = :o AND seq = :s"
            ),
            {"p": json.dumps(payload), "o": org, "s": seq},
        )
        assert result.rowcount == 1  # type: ignore[attr-defined]
        await conn.execute(
            text(f"ALTER TABLE audit_events ENABLE ALWAYS TRIGGER {IMMUTABLE_TRIGGER}")
        )
        await conn.commit()
    finally:
        await conn.close()


# ------------------------------------------------------------------ chain


async def test_append_and_verify(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 3)
    async with _session(app_engine, org) as s, s.begin():
        rows = (
            await s.execute(text("SELECT seq, prev_hash, hash FROM audit_events ORDER BY seq"))
        ).all()
    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[0].prev_hash == GENESIS_HASH
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash
    result = await _verify(app_engine, org)
    assert result.ok
    assert (result.checked, result.last_seq, result.last_hash) == (3, 3, rows[2].hash)


async def test_tampered_row_is_reported_at_its_seq(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 4)
    await _tamper(owner_engine, org, 2, {"i": 999})
    try:
        result = await _verify(app_engine, org)
        assert (result.ok, result.broken_seq, result.reason) == (False, 2, "hash_mismatch")
        assert result.checked == 1
    finally:
        await _tamper(owner_engine, org, 2, {"i": 1})
    assert (await _verify(app_engine, org)).ok


async def test_verify_needs_matching_tenant(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    async with _session(app_engine, tenants.a.org_id) as s, s.begin():
        with pytest.raises(TenantError):
            await verify_chain(s, tenants.b.org_id)


async def test_batched_verification(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 7)
    async with _session(app_engine, org) as s, s.begin():
        result = await verify_chain(s, org, batch_size=3)
        assert (result.ok, result.checked) == (True, 7)
        with pytest.raises(ValueError, match="batch_size"):
            await verify_chain(s, org, batch_size=0)


# ------------------------------------------------------------------ immutability


_STATEMENTS = {
    "update": "UPDATE audit_events SET action = 'core.x.y'",
    "delete": "DELETE FROM audit_events",
    "truncate": "TRUNCATE audit_events",
}


@pytest.mark.parametrize("op", sorted(_STATEMENTS))
async def test_app_role_cannot_change_events(
    app_engine: AsyncEngine, tenants: TwoTenants, op: str
) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 1)
    async with _session(app_engine, org) as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            async with s.begin():
                await s.execute(text(_STATEMENTS[op]))


@pytest.mark.parametrize("op", sorted(_STATEMENTS))
async def test_owner_cannot_change_events(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenants: TwoTenants, op: str
) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 1)
    conn = await _owner_tx(owner_engine, org)
    try:
        # The rows are visible to the owner here, so the row trigger really fires.
        count = (await conn.execute(text("SELECT count(*) FROM audit_events"))).scalar_one()
        assert count >= 1
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text(_STATEMENTS[op]))
    finally:
        await conn.rollback()
        await conn.close()
    assert (await _verify(app_engine, org)).checked == 1


async def test_triggers_fire_even_in_replica_mode(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    """ENABLE ALWAYS: a superuser's session_replication_role trick does not skip them."""
    async with owner_engine.connect() as conn:
        modes = (
            await conn.execute(
                text(
                    "SELECT tgname, tgenabled FROM pg_trigger "
                    "WHERE tgrelid = 'audit_events'::regclass AND NOT tgisinternal"
                )
            )
        ).all()
    assert {m.tgname: bytes(m.tgenabled).decode() for m in modes} == {
        "audit_events_immutable": "A",
        "audit_events_no_truncate": "A",
        "audit_events_link": "A",
    }


@pytest.mark.parametrize(
    ("seq", "prev"),
    [(1, GENESIS_HASH), (3, "a" * 64), (2, "b" * 64)],
    ids=["duplicate-genesis", "gap", "wrong-prev"],
)
async def test_insert_must_continue_the_chain(
    app_engine: AsyncEngine, tenants: TwoTenants, seq: int, prev: str
) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 1)
    async with _session(app_engine, org) as s:
        with pytest.raises(DBAPIError, match="audit chain"):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO audit_events (org_id, seq, occurred_at, actor_type, action, "
                        "prev_hash, hash) VALUES (:o, :s, now(), 'system', 'core.x.y', :p, :h)"
                    ),
                    {"o": org, "s": seq, "p": prev, "h": "c" * 64},
                )


# ------------------------------------------------------------------ isolation


async def test_cross_tenant_isolation(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    a, b = tenants.a.org_id, tenants.b.org_id
    await _append(app_engine, a, 2)
    await _append(app_engine, b, 3)
    async with _session(app_engine, a) as s, s.begin():
        orgs = set((await s.execute(text("SELECT org_id FROM audit_events"))).scalars())
        assert orgs == {a}
        assert (await s.execute(text("SELECT max(seq) FROM audit_events"))).scalar_one() == 2
        # Forging an event into B's chain is refused by RLS.
        with pytest.raises(DBAPIError, match="row-level security"):
            async with s.begin_nested():
                await s.execute(
                    text(
                        "INSERT INTO audit_events (org_id, seq, occurred_at, actor_type, action, "
                        "prev_hash, hash) VALUES (:o, 1, now(), 'system', 'core.x.y', :p, :h)"
                    ),
                    # Looks like a genesis event to the link trigger (which only sees A's
                    # rows), so the row level security check is what refuses it.
                    {"o": b, "p": GENESIS_HASH, "h": "d" * 64},
                )
    async with _session(app_engine, None) as s, s.begin():
        assert (await s.execute(text("SELECT count(*) FROM audit_events"))).scalar_one() == 0
        # Turning the owner-only scan flag on does nothing for the app role.
        await s.execute(text("SELECT set_config('app.audit_org_scan', 'on', true)"))
        assert (await s.execute(text("SELECT count(*) FROM audit_events"))).scalar_one() == 0
    assert (await _verify(app_engine, a)).checked == 2
    assert (await _verify(app_engine, b)).checked == 3


async def test_audit_org_ids_returns_ids_only(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    await _append(app_engine, tenants.a.org_id, 1)
    await _append(app_engine, tenants.b.org_id, 1)
    async with _session(app_engine, None) as s, s.begin():
        ids = set((await s.execute(text("SELECT * FROM audit_org_ids()"))).scalars())
        assert {tenants.a.org_id, tenants.b.org_id} <= ids
        # The flag is put back: rows stay hidden afterwards in the same transaction.
        flag = await s.execute(text("SELECT current_setting('app.audit_org_scan', true)"))
        assert not flag.scalar()
        assert (await s.execute(text("SELECT count(*) FROM audit_events"))).scalar_one() == 0


# ------------------------------------------------------------------ concurrency


async def test_concurrent_appends_keep_one_chain(migrated: DbUrls, tenants: TwoTenants) -> None:
    org = tenants.a.org_id
    engine = create_async_engine(migrated.app, pool_size=6, max_overflow=0)
    try:

        async def writer(worker: int) -> None:
            for i in range(5):
                async with _session(engine, org) as s, s.begin():
                    await append_audit(
                        s,
                        action="core.test.concurrent",
                        actor_type=ActorType.SYSTEM,
                        actor_id=f"w{worker}",
                        payload={"i": i},
                    )
                    # Hold the transaction open a moment so appends really overlap.
                    await asyncio.sleep(0.005)

        await asyncio.gather(*(writer(w) for w in range(6)))
        result = await _verify(engine, org)
    finally:
        await engine.dispose()
    assert (result.ok, result.checked, result.last_seq) == (True, 30, 30)


async def test_rolled_back_event_leaves_no_gap(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    org = tenants.a.org_id
    await _append(app_engine, org, 1)
    async with _session(app_engine, org) as s, s.begin():
        await append_audit(s, action="core.test.x", actor_type="system", actor_id="t")
        await s.rollback()
    await _append(app_engine, org, 1)
    result = await _verify(app_engine, org)
    assert (result.ok, result.last_seq) == (True, 2)


async def test_membership_change_helper(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    a = tenants.a
    registry = build_registry(PLUGINS)
    grants = (RoleGrant("admin"),)
    actor = Actor(ActorKind.USER, a.user_id, a.org_id, grants, registry.resolve(grants))
    async with _session(app_engine, a.org_id) as s, s.begin():
        record = await record_membership_change(
            s,
            actor,
            action=AuditAction.MEMBERSHIP_CHANGED,
            membership_id=a.membership_id,
            user_id=a.user_id,
            role_key="auditor",
            workflow=None,
            previous_role_key="approver",
        )
    assert record.entity_type == "membership"
    assert record.payload == {
        "user_id": str(a.user_id),
        "role": "auditor",
        "workflow": None,
        "previous_role": "approver",
    }
    assert (record.actor_type, record.actor_id) == ("user", str(a.user_id))


# ------------------------------------------------------------------ HTTP API


@dataclasses.dataclass(frozen=True)
class Member:
    org_id: uuid.UUID
    user_id: uuid.UUID
    subject: str


async def _add_member(owner: AsyncEngine, org: uuid.UUID, role: str) -> Member:
    user_id, subject = uuid.uuid4(), f"sub-{role}-{uuid.uuid4().hex[:8]}"
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('app.org_id', :o, true), "
                "set_config('app.auth_subject', :s, true)"
            ),
            {"o": str(org), "s": subject},
        )
        await conn.execute(
            text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
            {"id": user_id, "s": subject, "e": f"{role}@example.test"},
        )
        await conn.execute(
            text(
                "INSERT INTO memberships (org_id, user_id, role_id) "
                "SELECT :o, :u, r.id FROM roles r WHERE r.key = :r AND r.org_id IS NULL"
            ),
            {"o": org, "u": user_id, "r": role},
        )
    return Member(org, user_id, subject)


async def _remove_member(owner: AsyncEngine, member: Member) -> None:
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('app.org_id', :o, true), "
                "set_config('app.auth_subject', :s, true)"
            ),
            {"o": str(member.org_id), "s": member.subject},
        )
        await conn.execute(
            text("DELETE FROM memberships WHERE user_id = :u"), {"u": member.user_id}
        )
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": member.user_id})


@dataclasses.dataclass
class Live:
    client: httpx.AsyncClient
    tokens: TokenFactory

    def headers(self, member: Member) -> dict[str, str]:
        return {
            # Signed in with TOTP: exports need it (step-up rule, pairly_core.rbac).
            "Authorization": f"Bearer {self.tokens.token(member.subject, amr=['otp'])}",
            "X-Pairly-Org": str(member.org_id),
        }


@pytest.fixture
async def live(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[Live]:
    app = create_app(make_settings(migrated.app), plugins=PLUGINS)

    @app.post("/v1/test/rename")
    async def rename(
        actor: Annotated[Actor, Depends(require("core.org.manage"))], session: OrgSession
    ) -> dict[str, str]:
        org = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
        assert org is not None
        org.name = org.name + " (renamed)"
        return {"ok": "yes"}

    @app.post("/v1/test/rename-audited")
    async def rename_audited(
        actor: Annotated[Actor, Depends(require("core.org.manage"))], session: OrgSession
    ) -> dict[str, str]:
        org = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
        assert org is not None
        org.name = org.name + " (again)"
        await audit_actor_action(
            session, actor, "core.org.renamed", entity_type="organisation", entity_id=org.id
        )
        return {"ok": "yes"}

    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield Live(c, tokens)


@pytest.fixture
async def members(
    owner_engine: AsyncEngine, tenants: TwoTenants
) -> AsyncIterator[dict[str, Member]]:
    org = tenants.a.org_id
    created = {
        role: await _add_member(owner_engine, org, role)
        for role in ("auditor", "finance_controller", "admin")
    }
    a = tenants.a
    created["approver"] = Member(a.org_id, a.user_id, a.subject)
    yield created
    for role in ("auditor", "finance_controller", "admin"):
        await _remove_member(owner_engine, created[role])


async def _events(app_engine: AsyncEngine, org: uuid.UUID, action: str) -> list[AuditRecord]:
    async with _session(app_engine, org) as s, s.begin():
        rows = (
            await s.execute(
                text("SELECT * FROM audit_events WHERE action = :a ORDER BY seq"), {"a": action}
            )
        ).all()
    return [AuditRecord.from_row(r) for r in rows]


@pytest.mark.parametrize(
    ("role", "path", "status"),
    [
        ("auditor", "/v1/audit/events", 200),
        ("auditor", "/v1/audit/verify", 200),
        ("auditor", "/v1/audit/export?format=json", 200),
        ("finance_controller", "/v1/audit/events", 200),
        ("finance_controller", "/v1/audit/export?format=csv", 403),
        ("approver", "/v1/audit/events", 403),
        ("approver", "/v1/audit/verify", 403),
        ("approver", "/v1/audit/export?format=csv", 403),
        ("admin", "/v1/audit/export?format=csv", 200),
    ],
)
async def test_audit_endpoints_rbac(
    live: Live, members: dict[str, Member], role: str, path: str, status: int
) -> None:
    response = await live.client.get(path, headers=live.headers(members[role]))
    assert response.status_code == status, response.text


async def test_auditor_is_read_only(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    auditor = members["auditor"]
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await live.client.request(
            method, "/v1/audit/events", headers=live.headers(auditor)
        )
        assert response.status_code == 405
    response = await live.client.post("/v1/test/rename", headers=live.headers(auditor))
    assert response.status_code == 403


async def test_denials_are_audited(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    approver = members["approver"]
    response = await live.client.get(
        "/v1/audit/events",
        headers=live.headers(approver) | {"X-Request-ID": "req-denied-0001"},
    )
    assert response.status_code == 403
    denied = await _events(app_engine, approver.org_id, AuditAction.AUTHZ_DENIED)
    assert len(denied) == 1
    event = denied[0]
    assert (event.actor_type, event.actor_id) == ("user", str(approver.user_id))
    assert event.payload == {"permission": "core.audit.read", "reason": "missing_permission"}
    assert event.request_id == "req-denied-0001"


async def test_reads_are_not_audited(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    auditor = members["auditor"]
    for path in ("/v1/me", "/v1/audit/events", "/v1/audit/verify"):
        assert (await live.client.get(path, headers=live.headers(auditor))).status_code == 200
    async with _session(app_engine, auditor.org_id) as s, s.begin():
        assert (await s.execute(text("SELECT count(*) FROM audit_events"))).scalar_one() == 0


async def test_unaudited_write_fails_in_test_env(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    # Development and test: a write without its own audit event fails and rolls back
    # (production writes a generic core.request.write event instead, see
    # test_audit_hardening.py).
    admin = members["admin"]
    response = await live.client.post("/v1/test/rename", headers=live.headers(admin))
    assert response.status_code == 500
    assert response.json()["code"] == "unaudited_write"
    assert await _events(app_engine, admin.org_id, AuditAction.REQUEST_WRITE) == []

    # A route that records its own event passes.
    response = await live.client.post("/v1/test/rename-audited", headers=live.headers(admin))
    assert response.is_success
    assert await _events(app_engine, admin.org_id, AuditAction.REQUEST_WRITE) == []
    renamed = await _events(app_engine, admin.org_id, "core.org.renamed")
    assert [e.entity_id for e in renamed] == [str(admin.org_id)]


async def test_list_filters_and_pagination(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    auditor = members["auditor"]
    org = auditor.org_id
    await _append(app_engine, org, 5, action="core.test.alpha")
    await _append(app_engine, org, 2, action="core.test.beta")
    headers = live.headers(auditor)

    seen: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 3, "action": "core.test.alpha"}
        if cursor:
            params["cursor"] = cursor
        page = (await live.client.get("/v1/audit/events", params=params, headers=headers)).json()
        seen += [item["seq"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [5, 4, 3, 2, 1]

    asc = await live.client.get(
        "/v1/audit/events", params={"order": "asc", "limit": 2}, headers=headers
    )
    body = asc.json()
    assert [i["seq"] for i in body["items"]] == [1, 2]
    assert body["items"][0]["prev_hash"] == GENESIS_HASH
    nxt = await live.client.get(
        "/v1/audit/events",
        params={"order": "asc", "limit": 2, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert [i["seq"] for i in nxt.json()["items"]] == [3, 4]

    # A cursor from the other sort order, or garbage, is rejected.
    for bad in (body["next_cursor"], "!!!", "djE6YXNjOng"):
        response = await live.client.get(
            "/v1/audit/events", params={"order": "desc", "cursor": bad}, headers=headers
        )
        assert response.status_code == 400

    beta = await live.client.get(
        "/v1/audit/events",
        params={"action": "core.test.beta", "actor_type": "system", "entity_id": "t-1"},
        headers=headers,
    )
    assert [i["seq"] for i in beta.json()["items"]] == [7]
    naive = await live.client.get(
        "/v1/audit/events", params={"from": "2026-01-01T00:00:00"}, headers=headers
    )
    assert naive.status_code == 400
    window = await live.client.get(
        "/v1/audit/events",
        params={"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T00:00:00Z"},
        headers=headers,
    )
    assert window.json()["items"] == []


async def test_verify_endpoint_reports_tampering(
    live: Live,
    members: dict[str, Member],
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
) -> None:
    auditor = members["auditor"]
    org = auditor.org_id
    await _append(app_engine, org, 3)
    ok = (await live.client.get("/v1/audit/verify", headers=live.headers(auditor))).json()
    assert ok["ok"] is True
    assert ok["checked"] == 3
    await _tamper(owner_engine, org, 3, {"i": 42})
    try:
        bad = (await live.client.get("/v1/audit/verify", headers=live.headers(auditor))).json()
        assert (bad["ok"], bad["broken_seq"], bad["reason"]) == (False, 3, "hash_mismatch")
    finally:
        await _tamper(owner_engine, org, 3, {"i": 2})


async def test_json_export_verifies_offline(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine
) -> None:
    auditor = members["auditor"]
    await _append(app_engine, auditor.org_id, 450)  # more than one streamed chunk
    response = await live.client.get(
        "/v1/audit/export", params={"format": "json"}, headers=live.headers(auditor)
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["format"] == "pairly.audit.v1"
    assert body["org_id"] == str(auditor.org_id)
    records = [AuditRecord.from_json(item) for item in body["events"]]
    # The export records itself first, so it is part of what it exports.
    assert records[-1].action == AuditAction.AUDIT_EXPORTED
    assert records[-1].payload == {"format": "json", "filters": {}}
    result = verify_records(auditor.org_id, records)
    assert (result.ok, result.checked) == (True, 451)


async def test_csv_export(live: Live, members: dict[str, Member], app_engine: AsyncEngine) -> None:
    auditor = members["auditor"]
    org = auditor.org_id
    async with _session(app_engine, org) as s, s.begin():
        await append_audit(
            s,
            action="core.test.csv",
            actor_type="system",
            actor_id="tests",
            entity_id="=cmd",
            payload={"note": 'a,b "quoted"', "n": 1},
        )
    response = await live.client.get(
        "/v1/audit/export",
        params={"format": "csv", "action": "core.test.csv"},
        headers=live.headers(auditor),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:3] == ["seq", "id", "org_id"]
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["entity_id"] == "'=cmd"
    assert json.loads(row["payload"]) == {"note": 'a,b "quoted"', "n": 1}
    assert row["seq"] == "1"
    exported = await _events(app_engine, org, AuditAction.AUDIT_EXPORTED)
    assert exported[-1].payload == {"format": "csv", "filters": {"action": "core.test.csv"}}


async def test_export_is_tenant_scoped(
    live: Live, members: dict[str, Member], app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    await _append(app_engine, tenants.b.org_id, 3)
    auditor = members["auditor"]
    response = await live.client.get(
        "/v1/audit/export", params={"format": "json"}, headers=live.headers(auditor)
    )
    orgs = {e["org_id"] for e in response.json()["events"]}
    assert orgs == {str(auditor.org_id)}
    # Asking for another org with this token is refused before any audit query.
    foreign = live.headers(auditor) | {"X-Pairly-Org": str(tenants.b.org_id)}
    assert (await live.client.get("/v1/audit/events", headers=foreign)).status_code == 403

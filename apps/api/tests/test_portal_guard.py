# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The portal guard (migration 0022) on Postgres: defence in depth for portal sessions.

* Every org-scoped table in the database (core, every plugin) and ``users`` carries a
  restrictive portal policy: core's ``<table>_portal_guard`` or a plugin's own
  ``<table>_portal_subject``. A new table without one fails here.
* After real portal flows (a vendor contact uploads an invoice, writes to AP and asks
  for a bank change; a candidate applies, signs in and reads their timeline and privacy
  centre), the database itself shows a portal session of anyone else (another vendor,
  another candidate) no row of any org-scoped table but the shared templates and Pay's
  settings; and the portal person themselves sees, of core tables, only what the narrow
  rules allow, tied to them: their own uploads, their own decisions and appeals, the
  rubric of a decision about them, the email texts. Writes outside the rules are
  refused.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_portal_subject, set_tenant
from pairly_hire.portal.auth import portal_subject as candidate_subject
from pairly_pay.portal.auth import portal_subject as vendor_subject

from .hire_support import HireApi, HireWorld, hire_api, open_job
from .pay_flow_support import Flow, flow, fother, fworld, ready_invoice
from .pay_support import PayWorld, other_bank
from .test_hire_pipeline import other, world
from .test_hire_portal import _apply
from .test_hire_portal import _signed_in as candidate_signed_in
from .test_pay_vendor_portal import PORTAL, _grant, _portal_upload
from .test_pay_vendor_portal import _signed_in as vendor_signed_in

__all__ = ["flow", "fother", "fworld", "hire_api", "other", "world"]  # fixtures

# Readable by any portal session: shared email texts, and Pay's settings (the cooling
# off period a bank change needs, pay_0012).
ANY_PORTAL = frozenset({"templates", "pay_settings"})
# Core tables a portal person may see rows of, each by a narrow rule tied to them.
CANDIDATE_CORE = frozenset(
    {"documents", "decision_records", "appeals", "policy_versions", "templates"}
)
VENDOR_CORE = frozenset({"documents", "decision_records", "templates"})


async def _org_scoped(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND EXISTS ("
                "SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                "AND a.attname = 'org_id' AND NOT a.attisdropped) ORDER BY 1"
            )
        )
        return [str(r[0]) for r in rows]


async def _core_tables(engine: AsyncEngine) -> set[str]:
    """Org-scoped tables no plugin owns (no ``hire_``, ``pay_``, ``expense_`` prefix)."""
    return {t for t in await _org_scoped(engine) if not t.startswith(("hire_", "pay_", "expense_"))}


async def _visible(engine: AsyncEngine, org_id: uuid.UUID, subject: str | None) -> dict[str, int]:
    """Rows of every org-scoped table (and ``users``) a session sees."""
    tables = [*await _org_scoped(engine), "users"]
    session = create_sessionmaker(engine)()
    set_tenant(session, org_id, None)
    set_portal_subject(session, subject)
    out: dict[str, int] = {}
    async with session, session.begin():
        for table in tables:
            out[table] = int(
                (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            )
    return out


async def _refused(engine: AsyncEngine, org_id: uuid.UUID, subject: str, sql: str) -> None:
    session = create_sessionmaker(engine)()
    set_tenant(session, org_id, None)
    set_portal_subject(session, subject)
    with pytest.raises(Exception, match="row-level security"):
        async with session, session.begin():
            await session.execute(text(sql), {"o": org_id})


def _only(seen: dict[str, int], allowed: frozenset[str]) -> dict[str, int]:
    return {t: n for t, n in seen.items() if n and t not in allowed}


async def _strangers_see_nothing(engine: AsyncEngine, org_id: uuid.UUID) -> None:
    for stranger in (
        candidate_subject(uuid.uuid4()),
        vendor_subject(uuid.uuid4()),
    ):
        assert _only(await _visible(engine, org_id, stranger), ANY_PORTAL) == {}, stranger


# ------------------------------------------------------------------ every table


async def test_every_org_scoped_table_has_a_portal_policy(owner_engine: AsyncEngine) -> None:
    tables = [*await _org_scoped(owner_engine), "users"]
    assert len(tables) > 100
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
                "AND permissive = 'RESTRICTIVE' "
                "AND (policyname LIKE '%\\_portal\\_guard' "
                "OR policyname LIKE '%\\_portal\\_subject')"
            )
        )
        guarded = {str(r[0]) for r in rows}
        rules = (
            await conn.execute(text("SELECT DISTINCT table_name FROM portal_guard_rules"))
        ).all()
    assert [t for t in tables if t not in guarded] == []
    # Core's narrow exceptions are only on the tables the portal flows need.
    assert {str(r[0]) for r in rules} <= {
        "appeals",
        "audit_events",
        "decision_records",
        "documents",
        "hire_jobs",
        "hire_pipeline_configs",
        "policy_versions",
        "templates",
        "webhook_outbox",
    }


async def test_the_rules_table_is_closed_to_the_application(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute(text("SELECT * FROM portal_guard_rules"))


# ------------------------------------------------------------------ vendor portal


async def test_a_vendor_portal_session_sees_no_core_row_but_its_own(
    flow: Flow, fworld: PayWorld
) -> None:
    invoice_id, vendor_id = await ready_invoice(flow, fworld, "inv-0001")
    email = await _grant(flow, fworld, vendor_id)
    session = await vendor_signed_in(flow, fworld, email)
    uploaded = await _portal_upload(flow, session, "inv-0003")
    assert uploaded.status_code == 202, uploaded.text
    wrote = await flow.client.post(
        f"{PORTAL}/invoices/{invoice_id}/messages", json={"text": "Paid when?"}, headers=session
    )
    assert wrote.status_code == 201, wrote.text
    asked = await flow.client.post(f"{PORTAL}/bank-change", json=other_bank(), headers=session)
    assert asked.status_code == 201, asked.text

    staff = await _visible(flow.engine, fworld.org_id, None)
    for table in ("documents", "audit_events", "decision_records", "workflow_runs"):
        assert staff[table] > 0, table
    await _strangers_see_nothing(flow.engine, fworld.org_id)

    me = vendor_subject(uuid.UUID(vendor_id))
    seen = await _visible(flow.engine, fworld.org_id, me)
    core = await _core_tables(flow.engine)
    assert {t for t, n in seen.items() if n and t in core | {"users"}} <= VENDOR_CORE
    assert seen["documents"] == 1  # its own upload only, not the email intake's file
    assert seen["audit_events"] == 0
    session_db = create_sessionmaker(flow.engine)()
    set_tenant(session_db, fworld.org_id, None)
    set_portal_subject(session_db, me)
    async with session_db, session_db.begin():
        docs = (await session_db.execute(text("SELECT portal_subject FROM documents"))).all()
        actors = (
            await session_db.execute(text("SELECT DISTINCT actor_type FROM decision_records"))
        ).all()
    assert {r.portal_subject for r in docs} == {me}
    assert {r.actor_type for r in actors} == {"vendor"}

    # Writes outside the rules are refused by the database itself.
    await _refused(
        flow.engine,
        fworld.org_id,
        me,
        "INSERT INTO notification_preferences (org_id, user_id) VALUES (:o, gen_random_uuid())",
    )
    await _refused(
        flow.engine,
        fworld.org_id,
        me,
        "INSERT INTO documents (org_id, workflow, source, media_type, size_bytes, sha256, "  # noqa: S608 (constant SQL, never user input)
        "storage_key, status, portal_subject) VALUES (:o, 'pay', 'upload', 'application/pdf', "
        "1, repeat('a', 64), 'k', 'received', 'pay_vendor:" + str(uuid.uuid4()) + "')",
    )


# ------------------------------------------------------------------ candidate portal


async def test_a_candidate_portal_session_sees_no_core_row_but_its_own(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Lina Haddad")
    await _apply(api, world, job, "Omar Khalil")
    session = await candidate_signed_in(api, world, email, tmp_path)
    for path in ("/v1/hire/portal/applications", "/v1/hire/portal/privacy/export"):
        response = await api.client.get(path, headers=session)
        assert response.status_code == 200, response.text

    staff = await _visible(api.engine, world.org_id, None)
    for table in ("documents", "audit_events", "hire_applications", "memberships"):
        assert staff[table] > 0, table
    await _strangers_see_nothing(api.engine, world.org_id)

    lookup = create_sessionmaker(api.engine)()
    set_tenant(lookup, world.org_id, None)
    async with lookup, lookup.begin():
        candidate_id = (
            await lookup.execute(
                text("SELECT candidate_id FROM hire_applications WHERE id = :a"), {"a": app_id}
            )
        ).scalar_one()
    me = candidate_subject(candidate_id)
    seen = await _visible(api.engine, world.org_id, me)
    core = await _core_tables(api.engine)
    assert {t for t, n in seen.items() if n and t in core | {"users"}} <= CANDIDATE_CORE
    assert seen["documents"] == 1  # their own CV, never the other applicant's
    assert seen["hire_applications"] == 1
    assert seen["hire_jobs"] == 1
    assert seen["audit_events"] == seen["memberships"] == seen["users"] == 0

    await _refused(
        api.engine,
        world.org_id,
        me,
        "INSERT INTO memberships (org_id, user_id, role_id) "
        "VALUES (:o, gen_random_uuid(), gen_random_uuid())",
    )

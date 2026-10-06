# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The worker's nightly audit verification job against a real Postgres.

Kept with the API database tests to reuse their migration and tenant fixtures.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.audit import AuditAction
from pairly_core.db import UnsafeDatabaseRoleError, create_sessionmaker
from pairly_worker.config import load_worker_config
from pairly_worker.jobs import SYSTEM_ACTOR_ID, list_audit_orgs, verify_audit_chains
from pairly_worker.main import shutdown, startup

from .conftest import DbUrls, TwoTenants
from .test_audit import _append, _session, _tamper, _verify


async def _broken_events(engine: AsyncEngine, org: uuid.UUID) -> list[Any]:
    async with _session(engine, org) as s, s.begin():
        return list(
            (
                await s.execute(
                    text("SELECT * FROM audit_events WHERE action = :a"),
                    {"a": AuditAction.AUDIT_CHAIN_BROKEN},
                )
            ).all()
        )


async def test_nightly_job_flags_broken_chains(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    tenants: TwoTenants,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, b = tenants.a.org_id, tenants.b.org_id
    await _append(app_engine, a, 3)
    await _append(app_engine, b, 2)
    ctx: dict[str, Any] = {"sessionmaker": create_sessionmaker(app_engine)}

    assert {a, b} <= set(await list_audit_orgs(ctx["sessionmaker"]))
    clean = await verify_audit_chains(ctx)
    assert str(a) not in clean["broken"]
    assert str(b) not in clean["broken"]

    await _tamper(owner_engine, a, 2, {"i": 7})
    try:
        summary = await verify_audit_chains(ctx)
        assert str(a) in summary["broken"]
        assert str(b) not in summary["broken"]
        (event,) = await _broken_events(app_engine, a)
        assert (event.actor_type, event.actor_id) == ("system", SYSTEM_ACTOR_ID)
        assert event.payload == {"broken_seq": 2, "reason": "hash_mismatch", "checked": 1}
        assert event.seq == 4  # appended to the same chain, after the stored head
        assert await _broken_events(app_engine, b) == []
    finally:
        await _tamper(owner_engine, a, 2, {"i": 1})
    assert (await _verify(app_engine, a)).ok


async def test_worker_startup_checks_role(migrated: DbUrls, owner_engine: AsyncEngine) -> None:
    config = load_worker_config(database_url=migrated.app, redis_url="redis://localhost:6379/0")
    ctx: dict[str, Any] = {"config": config}
    await startup(ctx)
    try:
        async with ctx["sessionmaker"]() as s, s.begin():
            assert (await s.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await shutdown(ctx)
    assert "engine" not in ctx

    async with owner_engine.connect() as owner:
        is_super = (
            await owner.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
    if is_super:
        bad: dict[str, Any] = {
            "config": load_worker_config(
                database_url=migrated.owner, redis_url="redis://localhost:6379/0"
            )
        }
        with pytest.raises(UnsafeDatabaseRoleError):
            await startup(bad)

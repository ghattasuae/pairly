# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Database helpers that do not need a running Postgres (the real RLS tests live in
apps/api/tests/test_tenant_isolation.py)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core import db
from pairly_core.db import TenantError, create_sessionmaker, current_org, set_tenant
from pairly_core.migration_helpers import app_db_role, ident, org_rls_statements
from pairly_core.models import Membership, Organisation

A, B = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def sessionmaker() -> Any:
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
    return create_sessionmaker(engine)


async def test_set_tenant(sessionmaker: Any) -> None:
    session = sessionmaker()
    assert current_org(session) is None
    set_tenant(session, A, "sub")
    assert current_org(session) == A
    with pytest.raises(TenantError, match="UUID"):
        set_tenant(session, "not-a-uuid", None)  # type: ignore[arg-type]


@dataclass
class _FakeSession:
    info: dict[str, Any]
    new: list[Any] = field(default_factory=list)
    dirty: list[Any] = field(default_factory=list)
    deleted: list[Any] = field(default_factory=list)


def _guard(org: uuid.UUID | None, **lists: list[Any]) -> None:
    db._guard_flush(_FakeSession(info={"pairly.org_id": org}, **lists), None, None)  # type: ignore[arg-type]


def test_flush_guard_assigns_and_checks_org() -> None:
    m = Membership(user_id=uuid.uuid4(), role_id=uuid.uuid4())
    _guard(A, new=[m])
    assert m.org_id == A
    with pytest.raises(TenantError):
        _guard(B, new=[m])
    with pytest.raises(TenantError):
        _guard(None, new=[Membership()])
    with pytest.raises(TenantError):
        _guard(B, dirty=[m])
    with pytest.raises(TenantError):
        _guard(B, deleted=[m])
    with pytest.raises(TenantError):
        _guard(B, dirty=[Organisation(id=A, name="x", slug="xx")])
    _guard(A, dirty=[m], deleted=[m])


def test_orm_criteria_rendered() -> None:
    """The ORM filter adds the org condition to queries (checked on compiled SQL)."""
    from sqlalchemy.orm import Session

    session = Session(bind=None)
    session.info["pairly.org_id"] = A

    class _State:
        is_select = True
        is_update = False
        is_delete = False

        def __init__(self) -> None:
            self.session = session
            self.statement = select(Membership)

    state = _State()
    db._add_tenant_criteria(state)  # type: ignore[arg-type]
    compiled = str(state.statement.compile(dialect=postgresql.dialect()))
    assert "memberships.org_id = " in compiled


def test_migration_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAIRLY_DB_APP_ROLE", raising=False)
    assert app_db_role() == "pairly_app"
    monkeypatch.setenv("PAIRLY_DB_APP_ROLE", "app; DROP TABLE x")
    with pytest.raises(ValueError, match="identifier"):
        app_db_role()
    stmts = org_rls_statements("invoices", "pairly_app")
    assert "FORCE ROW LEVEL SECURITY" in stmts[1]
    assert "app_current_org_id()" in stmts[2]
    assert stmts[-1] == "GRANT SELECT, INSERT, UPDATE, DELETE ON invoices TO pairly_app"
    assert "REVOKE ALL ON invoices FROM pairly_app" in stmts
    with pytest.raises(ValueError, match="identifier"):
        ident("Bad-Name")
    with pytest.raises(ValueError, match="privilege"):
        org_rls_statements("invoices", "pairly_app", privileges="ALL; DROP")

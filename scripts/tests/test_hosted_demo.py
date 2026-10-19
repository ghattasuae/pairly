# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tests for scripts/hosted_demo.py: the guards, and the wipe on a scratch schema."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hosted_demo.py"
OWNER = "postgresql://pairly_owner:pw@pairly-demo-db.internal:5432/pairly"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hosted_demo", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hosted_demo"] = module
    spec.loader.exec_module(module)
    return module


hd = _load()


def _env(**changes: str) -> dict[str, str]:
    env = {
        "PAIRLY_HOSTED_DEMO": "1",
        "PAIRLY_ENV": "staging",
        "PAIRLY_DATABASE_URL_OWNER": OWNER,
        "PAIRLY_HOSTED_DEMO_DATABASE": "pairly",
    }
    env.update(changes)
    return {k: v for k, v in env.items() if v != "-"}


def test_guards_pass_for_the_hosted_demo() -> None:
    hd.check_guards(_env(), wiping=True)
    hd.check_guards(_env(PAIRLY_HOSTED_DEMO_DATABASE="-"), wiping=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"PAIRLY_HOSTED_DEMO": "-"},
        {"PAIRLY_HOSTED_DEMO": "true"},
        {"PAIRLY_ENV": "production"},
        {"PAIRLY_ENV": "development"},
        {"PAIRLY_ENV": "-"},
        {"PAIRLY_DATABASE_URL_OWNER": "-"},
        {"PAIRLY_HOSTED_DEMO_DATABASE": "-"},
        {"PAIRLY_HOSTED_DEMO_DATABASE": "pairly_prod"},
    ],
)
def test_reset_refuses_anything_else(changes: dict[str, str]) -> None:
    with pytest.raises(SystemExit) as exc:
        hd.check_guards(_env(**changes), wiping=True)
    assert exc.value.code == 2


def test_main_refuses_before_touching_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(hd, "migrate", lambda env: called.append("migrate"))
    with pytest.raises(SystemExit):
        hd.main(["reset"], env=_env(PAIRLY_ENV="production"))
    with pytest.raises(SystemExit):
        hd.main(["migrate"], env=_env(PAIRLY_HOSTED_DEMO="-"))
    assert called == []
    assert hd.main(["migrate"], env=_env()) == 0
    assert called == ["migrate"]


def test_documents_bucket_is_never_the_anchor_bucket() -> None:
    env = _env(PAIRLY_S3_BUCKET="pairly-audit-anchors")
    with pytest.raises(SystemExit):
        hd.empty_documents_bucket(env)


def test_schema_name_is_checked() -> None:
    import asyncio

    with pytest.raises(ValueError, match="invalid schema"):
        asyncio.run(hd.wipe_schema(object(), "public; DROP"))


def test_auth_schema_is_configurable_and_checked() -> None:
    import asyncio

    assert hd.auth_schema({}) == "auth"
    assert hd.auth_schema({"PAIRLY_AUTH_DB_SCHEMA": " "}) == "auth"
    assert hd.auth_schema({"PAIRLY_AUTH_DB_SCHEMA": "pairly_auth"}) == "pairly_auth"
    for bad in ("public", "Pairly", 'a"b', "x; DROP"):
        with pytest.raises(SystemExit):
            hd.auth_schema({"PAIRLY_AUTH_DB_SCHEMA": bad})
    with pytest.raises(ValueError, match="invalid schema"):
        asyncio.run(hd.empty_auth_tables("postgresql://unused", 'auth"; DROP'))


def test_asyncpg_dsn_drops_the_node_only_option() -> None:
    url = "postgresql://u:p@h:5432/postgres?sslmode=require&uselibpqcompat=true"
    assert hd.asyncpg_dsn(url) == "postgresql://u:p@h:5432/postgres?sslmode=require"
    assert hd.asyncpg_dsn("postgresql://u:p@h/db?uselibpqcompat=true") == "postgresql://u:p@h/db"
    assert hd.asyncpg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


class _Rows:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str]]:
        return self._rows


class _Conn:
    def __init__(self, owned: list[str]) -> None:
        self.owned = owned
        self.sql: list[str] = []

    async def execute(self, statement: object) -> _Rows:
        self.sql.append(str(statement))
        return _Rows([(n,) for n in self.owned])


def test_owned_schemas_are_dropped_but_not_public() -> None:
    import asyncio

    conn = _Conn(["lg_checkpoints"])
    assert asyncio.run(hd.drop_owned_schemas(conn)) == ["lg_checkpoints"]
    assert "n.nspname <> 'public'" in conn.sql[0]
    assert conn.sql[1] == 'DROP SCHEMA "lg_checkpoints" CASCADE'
    with pytest.raises(RuntimeError, match="unexpected schema"):
        asyncio.run(hd.drop_owned_schemas(_Conn(['x"; DROP'])))


@pytest.mark.skipif(
    not os.environ.get("PAIRLY_TEST_DATABASE_URL_OWNER"),
    reason="needs PAIRLY_TEST_DATABASE_URL_OWNER (the owner role of a test database)",
)
async def test_wipe_drops_everything_the_owner_owns_in_the_schema() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from pairly_core.config import to_asyncpg_url

    schema = f"wipe_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(
        to_asyncpg_url(os.environ["PAIRLY_TEST_DATABASE_URL_OWNER"]), pool_size=1
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            for statement in (
                "CREATE TYPE {s}.mood AS ENUM ('ok', 'not_ok')",
                "CREATE DOMAIN {s}.positive AS integer CHECK (VALUE > 0)",
                "CREATE TYPE {s}.pair AS (a integer, b integer)",
                "CREATE TABLE {s}.parent (id serial PRIMARY KEY, m {s}.mood, p {s}.positive)",
                "CREATE TABLE {s}.child (id integer REFERENCES {s}.parent (id))",
                "ALTER TABLE {s}.child ENABLE ROW LEVEL SECURITY",
                "CREATE POLICY child_all ON {s}.child USING (true)",
                "CREATE VIEW {s}.parents AS SELECT id FROM {s}.parent",
                "CREATE SEQUENCE {s}.counter",
                "CREATE FUNCTION {s}.twice(x integer) RETURNS integer "
                "LANGUAGE sql AS 'SELECT x * 2'",
                "CREATE FUNCTION {s}.touch() RETURNS trigger LANGUAGE plpgsql "
                "AS 'BEGIN RETURN NEW; END'",
                "CREATE TRIGGER t BEFORE INSERT ON {s}.parent "
                "FOR EACH ROW EXECUTE FUNCTION {s}.touch()",
            ):
                await conn.execute(text(statement.format(s=schema)))
        async with engine.begin() as conn:
            dropped = await hd.wipe_schema(conn, schema)
            assert dropped >= 8
            left = (await conn.execute(text(hd.OWNED_SQL), {"schema": schema})).all()
            assert left == []
            # The schema itself stays (the owner's grants on it are unchanged).
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": schema}
                )
                == 1
            )
            # A second wipe of an empty schema is a no-op.
            assert await hd.wipe_schema(conn, schema) == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()

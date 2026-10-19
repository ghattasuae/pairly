#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The hosted demo's database jobs (docs/hosted-demo.md). Runs in the ``pairly-demo-ops``
Fly app, from the ``demo`` image target of ``infra/docker/python.Dockerfile``.

    python scripts/hosted_demo.py migrate   # Alembic upgrade heads, as the owner role
    python scripts/hosted_demo.py reset     # a fresh demo: wipe, migrate, seed, flag on

``reset`` (the first seed and the nightly job) gives visitors a clean demo every day,
whatever they did the day before:

1. Wipe: every table, view, sequence, function and type the owner role owns in schema
   ``public`` is dropped (Alembic's version table included), and so is every other
   schema the owner role owns (``lg_checkpoints``, which migration 0006 creates again).
   Extension objects (pgvector, created by the superuser in ``infra/postgres/init`` or
   by ``infra/supabase/bootstrap.sql``) and schema ``public`` itself stay, so the owner
   role needs no extra right.
2. Migrate: ``alembic upgrade heads`` on the empty schema.
3. Sign-in data: every Better Auth table in the auth schema (``PAIRLY_AUTH_DB_SCHEMA``:
   ``auth`` on the Fly Postgres, ``pairly_auth`` on Supabase) is emptied (users,
   sessions, magic link tokens, rate limit rows), as ``pairly_auth``. The signing keys (``jwks``)
   stay, so the API's cached key set stays valid.
4. Queue: the job queue's Valkey database is flushed (jobs of the old data would fail).
5. Documents: every object in the documents bucket is deleted. The audit anchor bucket is
   left alone: anchors are write once.
6. Seed: ``scripts/demo_seed.py`` (it needs the API, the worker and the web app running,
   and calls the public careers API like a candidate would).
7. The demo organisation's admin turns on ``demo.public_signin`` (audited), so the
   public demo sign-in page offers the seeded users.

Fail closed. Both commands refuse unless ``PAIRLY_HOSTED_DEMO=1`` and ``PAIRLY_ENV`` is
``staging``; ``reset`` also needs ``PAIRLY_HOSTED_DEMO_DATABASE`` equal to the database
name in ``PAIRLY_DATABASE_URL_OWNER``, so a mistyped URL can never wipe another database.

The seed refuses to run outside development and test (it is a development seed). Its
process alone gets ``PAIRLY_ENV=development`` and ``PAIRLY_DEVSEED=1``; the API, worker
and web app keep running as ``staging`` with every production check on.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess  # nosec B404 (fixed commands, no shell)
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[1]
ALEMBIC_INI: Final = ROOT / "apps" / "api" / "alembic.ini"
SEED: Final = ROOT / "scripts" / "demo_seed.py"
ORG_SLUG: Final = "demo"
ADMIN_EMAIL: Final = "demo-admin@example.test"
KEEP_AUTH_TABLES: Final = frozenset({"jwks"})
SAFE_NAME: Final = r"^[a-z_][a-z0-9_]{0,62}$"


class RefusedError(SystemExit):
    def __init__(self, message: str) -> None:
        print(f"hosted demo: {message}", file=sys.stderr)
        super().__init__(2)


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RefusedError(f"{name} is not set")
    return value


def check_guards(env: Mapping[str, str], *, wiping: bool) -> None:
    """Raise ``RefusedError`` unless this is the hosted demo (see the module docstring)."""
    if env.get("PAIRLY_HOSTED_DEMO") != "1":
        raise RefusedError("PAIRLY_HOSTED_DEMO=1 is required (hosted demo only)")
    if env.get("PAIRLY_ENV") != "staging":
        raise RefusedError("PAIRLY_ENV must be staging for the hosted demo")
    owner = _require(env, "PAIRLY_DATABASE_URL_OWNER")
    if wiping:
        expected = _require(env, "PAIRLY_HOSTED_DEMO_DATABASE")
        if database_name(owner) != expected:
            raise RefusedError(
                "PAIRLY_HOSTED_DEMO_DATABASE does not match the database in "
                "PAIRLY_DATABASE_URL_OWNER; nothing was changed"
            )


def database_name(url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).database or ""


# ------------------------------------------------------------------ 1. wipe


# Everything the current role owns in one schema, minus extension members, as
# (kind, qualified name). Tables first: dropping them (CASCADE) takes their views,
# triggers, policies and owned sequences with them.
OWNED_SQL: Final = """
SELECT kind, name FROM (
  SELECT CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'v' THEN 'VIEW'
              WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE'
              WHEN 'f' THEN 'FOREIGN TABLE' END AS kind,
         format('%I.%I', n.nspname, c.relname) AS name,
         CASE WHEN c.relkind IN ('r', 'p') THEN 0 ELSE 1 END AS step
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
     AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.deptype = 'e')
  UNION ALL
  SELECT 'ROUTINE', p.oid::regprocedure::text, 2
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = :schema
     AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                        AND d.deptype = 'e')
  UNION ALL
  SELECT CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
         format('%I.%I', n.nspname, t.typname), 3
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
   WHERE n.nspname = :schema AND t.typtype IN ('e', 'c', 'd', 'r')
     AND (t.typrelid = 0 OR (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c')
     AND t.typowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid
                        AND d.deptype = 'e')
) owned
ORDER BY step, name
"""


OWNED_SCHEMAS_SQL: Final = """
SELECT n.nspname FROM pg_namespace n
 WHERE n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
   AND n.nspname <> 'public'
 ORDER BY n.nspname
"""


async def drop_owned_schemas(conn: Any) -> list[str]:
    """Drop every schema the connected role owns except ``public`` (``lg_checkpoints``:
    migration 0006 creates it, so it must not survive the wipe)."""
    import re

    from sqlalchemy import text

    names = [str(r[0]) for r in (await conn.execute(text(OWNED_SCHEMAS_SQL))).all()]
    for name in names:
        if not re.fullmatch(SAFE_NAME, name):
            raise RuntimeError("unexpected schema name owned by the owner role")
        await conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))  # nosec B608
    return names


async def wipe_schema(conn: Any, schema: str = "public", *, passes: int = 5) -> int:
    """Drop everything the connected role owns in ``schema`` (not extension objects).
    Returns how many objects were dropped. Several passes, since a CASCADE in one pass
    can already have removed an object listed for it."""
    import re

    from sqlalchemy import text

    if not re.fullmatch(SAFE_NAME, schema):
        raise ValueError("invalid schema name")
    dropped = 0
    for _ in range(passes):
        rows = (await conn.execute(text(OWNED_SQL), {"schema": schema})).all()
        if not rows:
            return dropped
        for kind, name in rows:
            # The name comes from the catalog, quoted by format('%I') or regprocedure.
            await conn.execute(text(f"DROP {kind} IF EXISTS {name} CASCADE"))  # nosec B608
            dropped += 1
    left = (await conn.execute(text(OWNED_SQL), {"schema": schema})).all()
    if left:
        raise RuntimeError(f"{len(left)} objects are still there after the wipe")
    return dropped


# ------------------------------------------------------------------ 2. migrate


def migrate(env: Mapping[str, str]) -> None:
    command = [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "heads"]
    subprocess.run(  # noqa: S603  # nosec B603 (fixed command)
        command, check=True, env={**env}, cwd=ALEMBIC_INI.parent
    )


# ------------------------------------------------------------------ 3. sign-in data


def auth_schema(env: Mapping[str, str]) -> str:
    """Better Auth's schema (``PAIRLY_AUTH_DB_SCHEMA``, default ``auth``), validated."""
    import re

    name = env.get("PAIRLY_AUTH_DB_SCHEMA", "").strip() or "auth"
    if not re.fullmatch(SAFE_NAME, name) or name == "public":
        raise RefusedError("PAIRLY_AUTH_DB_SCHEMA must be a lower-case schema name")
    return name


def asyncpg_dsn(url: str) -> str:
    """``url`` without the node-postgres only ``uselibpqcompat`` option (asyncpg would send
    it to the server as a setting, which Postgres refuses)."""
    from urllib.parse import parse_qsl, urlencode

    base, sep, query = url.partition("?")
    if not sep:
        return url
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "uselibpqcompat"]
    return f"{base}?{urlencode(kept)}" if kept else base


async def empty_auth_tables(auth_url: str, schema: str = "auth") -> list[str]:
    import re

    import asyncpg  # type: ignore[import-untyped]

    if not re.fullmatch(SAFE_NAME, schema):
        raise ValueError("invalid schema name")
    conn = await asyncpg.connect(asyncpg_dsn(auth_url))
    try:
        names = [
            str(r["tablename"])
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = $1 "
                "AND tableowner = current_user ORDER BY tablename",
                schema,
            )
        ]
        targets = [n for n in names if n not in KEEP_AUTH_TABLES]
        if targets:
            quoted = ", ".join(f'"{schema}"."{n}"' for n in targets if '"' not in n)
            await conn.execute(f"TRUNCATE {quoted} CASCADE")
        return targets
    finally:
        await conn.close()


# ------------------------------------------------------------------ 4. queue, 5. documents


async def flush_queue(redis_url: str) -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


def empty_documents_bucket(env: Mapping[str, str]) -> int:
    import boto3  # type: ignore[import-untyped]

    bucket = _require(env, "PAIRLY_S3_BUCKET")
    if bucket == env.get("PAIRLY_AUDIT_ANCHOR_BUCKET", "pairly-audit-anchors"):
        raise RefusedError("the documents bucket must not be the audit anchor bucket")
    client = boto3.client(
        "s3",
        endpoint_url=env.get("PAIRLY_S3_ENDPOINT_URL") or None,
        region_name=env.get("PAIRLY_S3_REGION", "auto"),
        aws_access_key_id=_require(env, "PAIRLY_S3_ACCESS_KEY"),
        aws_secret_access_key=_require(env, "PAIRLY_S3_SECRET_KEY"),
    )
    deleted = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            deleted += len(keys)
    return deleted


# ------------------------------------------------------------------ 6. seed, 7. flag


def seed(env: Mapping[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="pairly-demo-") as local_dir:
        seed_env = {
            **env,
            # The development seed's own guard; this process only (see the docstring).
            "PAIRLY_ENV": "development",
            "PAIRLY_DEVSEED": "1",
            "PAIRLY_LOCAL_DIR": local_dir,
        }
        subprocess.run(  # noqa: S603  # nosec B603 (fixed command)
            [sys.executable, str(SEED)], check=True, env=seed_env, cwd=ROOT
        )


async def turn_on_public_signin(owner_url: str) -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine

    from pairly_api.admin import org_id_by_slug
    from pairly_core.config import to_asyncpg_url
    from pairly_core.crypto.keys import init_crypto
    from pairly_core.db import create_sessionmaker, set_tenant
    from pairly_core.flags.registry import DEMO_PUBLIC_SIGNIN, build_flag_registry
    from pairly_core.flags.service import set_flag
    from pairly_core.identity import load_actor
    from pairly_core.models import User
    from pairly_core.plugins import discover_plugins
    from pairly_core.rbac import build_registry

    init_crypto()
    plugins = list(discover_plugins().values())
    registry = build_registry(plugins)
    flags = build_flag_registry(plugins)
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as session, session.begin():
            org_id = await org_id_by_slug(session, ORG_SLUG)
        if not isinstance(org_id, uuid.UUID):
            raise RefusedError(f"no organisation {ORG_SLUG!r} after the seed")
        async with sessionmaker() as session:
            set_tenant(session, org_id, None)
            async with session.begin():
                subject = await session.scalar(
                    select(User.auth_subject).where(func.lower(User.email) == ADMIN_EMAIL)
                )
        if not isinstance(subject, str):
            raise RefusedError(f"no user {ADMIN_EMAIL} after the seed")
        async with sessionmaker() as session:
            actor = await load_actor(
                session, registry, subject=subject, org_id=org_id, amr=frozenset({"otp"})
            )
            await set_flag(session, actor, DEMO_PUBLIC_SIGNIN, True, registry=flags)
            await session.commit()
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ commands


async def reset(env: Mapping[str, str]) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from pairly_core.config import to_asyncpg_url

    owner_url = env["PAIRLY_DATABASE_URL_OWNER"]
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            dropped = await wipe_schema(conn, "public")
            schemas = await drop_owned_schemas(conn)
    finally:
        await engine.dispose()
    print(f"1. wipe: {dropped} objects dropped from schema public, schemas {schemas} dropped")
    migrate(env)
    print("2. migrate: done")
    emptied = await empty_auth_tables(_require(env, "PAIRLY_AUTH_DATABASE_URL"), auth_schema(env))
    print(f"3. sign-in data: {len(emptied)} auth tables emptied (jwks kept)")
    await flush_queue(_require(env, "PAIRLY_REDIS_URL"))
    print("4. queue: flushed")
    print(f"5. documents: {empty_documents_bucket(env)} objects deleted")
    seed(env)
    print("6. seed: done")
    await turn_on_public_signin(owner_url)
    print("7. demo.public_signin: on for the demo organisation")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/hosted_demo.py")
    parser.add_argument("command", choices=["migrate", "reset"])
    args = parser.parse_args(argv)
    environ = dict(os.environ if env is None else env)
    check_guards(environ, wiping=args.command == "reset")
    if args.command == "migrate":
        migrate(environ)
        print("migrate: done")
        return 0
    asyncio.run(reset(environ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

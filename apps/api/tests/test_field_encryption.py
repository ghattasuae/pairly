# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Field encryption against a real Postgres: ciphertext at rest, row and tenant binding,
blind index lookups, key rotation (resumable, audited), the 0008 data migration, and
startup refusing to run without keys."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import pairly_core.models  # noqa: F401
from pairly_core.config import to_asyncpg_url
from pairly_core.crypto import keys as keys_mod
from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.envelope import DecryptionError, kek_version
from pairly_core.crypto.keys import CryptoConfigError, KeyRing, install_keyring, load_keyring
from pairly_core.crypto.rotation import REWRAPPED_ACTION, crypto_org_ids, rotate_keys
from pairly_core.crypto.types import seal, unseal
from pairly_core.db import Base, create_sessionmaker, set_tenant
from pairly_core.ingestion.models import InboundEmail
from pairly_core.llm.store import DatabaseCallStore

from .conftest import ALEMBIC_INI, DbUrls, TwoTenants, make_settings


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture
def ring_v1() -> Iterator[KeyRing]:
    """A fresh key ring (KEK version 1) installed for the test, restored afterwards."""
    previous = keys_mod._keyring
    ring = load_keyring(
        {"PAIRLY_KEK_1": _key(), "PAIRLY_KEK_CURRENT": "1", "PAIRLY_BLIND_INDEX_KEY": _key()}
    )
    install_keyring(ring)
    yield ring
    install_keyring(previous)


@pytest.fixture
async def clean_rows(owner_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[TwoTenants]:
    yield tenants
    for tenant in (tenants.a, tenants.b):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(tenant.org_id)}
            )
            await conn.execute(text("DELETE FROM model_calls"))
            await conn.execute(text("DELETE FROM inbound_emails"))


def _session(engine: AsyncEngine, org: uuid.UUID) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, None)
    return session


def _call_values(org: uuid.UUID, marker: str, response: Any = None) -> dict[str, Any]:
    return {
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
        "request_hash": secrets.token_hex(32),
        "mode": "live",
        "status": "ok",
        "request": {"inputs": {"text": marker}, "settings": {}},
        "response": response,
    }


async def _insert_call(
    engine: AsyncEngine, org: uuid.UUID, marker: str, response: Any
) -> uuid.UUID:
    async with _session(engine, org) as s, s.begin():
        return await DatabaseCallStore().insert(s, _call_values(org, marker, response))


async def _raw(engine: AsyncEngine, org: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with _session(engine, org) as s, s.begin():
        return list((await s.execute(text(sql), params)).all())


async def test_model_calls_are_ciphertext_at_rest(
    app_engine: AsyncEngine, clean_rows: TwoTenants, ring_v1: KeyRing
) -> None:
    org = clean_rows.a.org_id
    call_id = await _insert_call(app_engine, org, "Jordan Example CV", {"label": "secret-out"})
    (row,) = await _raw(
        app_engine, org, "SELECT request, response FROM model_calls WHERE id = :id", id=call_id
    )
    for value in (row.request, row.response):
        assert value.startswith("v1:1:")
    assert "Jordan" not in row.request
    assert "secret-out" not in row.response
    async with _session(app_engine, org) as s, s.begin():
        stored = await DatabaseCallStore().get(s, call_id)
    assert stored is not None
    assert stored.request == {"inputs": {"text": "Jordan Example CV"}, "settings": {}}
    assert stored.response == {"label": "secret-out"}
    null_id = await _insert_call(app_engine, org, "no output", None)
    (null_row,) = await _raw(
        app_engine, org, "SELECT response FROM model_calls WHERE id = :id", id=null_id
    )
    assert null_row.response is None


async def test_ciphertext_swapped_between_rows_or_tenants_fails(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    clean_rows: TwoTenants,
    ring_v1: KeyRing,
) -> None:
    a, b = clean_rows.a.org_id, clean_rows.b.org_id
    first = await _insert_call(app_engine, a, "first", {"x": 1})
    second = await _insert_call(app_engine, a, "second", {"x": 2})
    other = await _insert_call(app_engine, b, "tenant b", {"x": 3})
    (token,) = await _raw(app_engine, a, "SELECT request FROM model_calls WHERE id = :id", id=first)
    # An attacker with database write access copies row 1's ciphertext into row 2 ...
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(a)})
        await conn.execute(
            text("UPDATE model_calls SET request = :t WHERE id = :id"),
            {"t": token.request, "id": second},
        )
    # ... and into another tenant's row.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b)})
        await conn.execute(
            text("UPDATE model_calls SET request = :t WHERE id = :id"),
            {"t": token.request, "id": other},
        )
    async with _session(app_engine, a) as s, s.begin():
        assert (await DatabaseCallStore().get(s, first)) is not None
        with pytest.raises(DecryptionError):
            await DatabaseCallStore().get(s, second)
    async with _session(app_engine, b) as s, s.begin():
        with pytest.raises(DecryptionError):
            await DatabaseCallStore().get(s, other)


async def test_sender_address_is_encrypted_with_a_blind_index(
    app_engine: AsyncEngine, clean_rows: TwoTenants, ring_v1: KeyRing
) -> None:
    org = clean_rows.a.org_id
    email_id = uuid.uuid4()
    async with _session(app_engine, org) as s, s.begin():
        s.add(
            InboundEmail(
                id=email_id,
                org_id=org,
                workflow="pay",
                message_id=f"<{uuid.uuid4()}@example.test>",
                raw_sha256="c" * 64,
                sender_address=seal(
                    InboundEmail,
                    "sender_address",
                    "Billing@Example-Supplies.test",
                    org_id=org,
                    row_id=email_id,
                ),
                sender_address_bidx=blind_index("email", "Billing@Example-Supplies.test", org),
            )
        )
    (raw,) = await _raw(
        app_engine, org, "SELECT sender_address FROM inbound_emails WHERE id = :id", id=email_id
    )
    assert raw.sender_address.startswith("v1:")
    assert "Billing" not in raw.sender_address
    lookup = blind_index("email", " billing@example-supplies.TEST ", org)
    async with _session(app_engine, org) as s, s.begin():
        found = (
            await s.scalars(select(InboundEmail).where(InboundEmail.sender_address_bidx == lookup))
        ).one()
        assert unseal(found, "sender_address") == "Billing@Example-Supplies.test"
    # Writing plaintext to the encrypted column is refused before it reaches the database.
    with pytest.raises(Exception, match="refusing to write plaintext"):
        async with _session(app_engine, org) as s, s.begin():
            s.add(
                InboundEmail(
                    org_id=org,
                    workflow="pay",
                    message_id="<plain@example.test>",
                    raw_sha256="d" * 64,
                    sender_address="plain@example.test",  # type: ignore[arg-type]
                )
            )


async def test_rotation_rewraps_in_audited_resumable_batches(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    clean_rows: TwoTenants,
    ring_v1: KeyRing,
) -> None:
    a, b = clean_rows.a.org_id, clean_rows.b.org_id
    ids = [await _insert_call(app_engine, a, f"call {i}", {"i": i}) for i in range(3)]
    other = await _insert_call(app_engine, b, "other tenant", None)
    # One corrupted row: it must be reported and left alone, not re-wrapped.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(a)})
        broken = await _insert_call_raw(conn, a, "v1:1:AAAA:AAAA:AAAA")

    ring_v2 = KeyRing(
        keks={**ring_v1.keks, "2": secrets.token_bytes(32)},
        current="2",
        blind_index_key=ring_v1.blind_index_key,
    )
    install_keyring(ring_v2)
    owner_sessions = create_sessionmaker(owner_engine)

    assert {a, b} <= set(await crypto_org_ids(owner_sessions))
    dry = await rotate_keys(owner_sessions, Base.metadata, org_ids=[a], dry_run=True)
    assert dry.pending == {"model_calls.request": 4, "model_calls.response": 3}
    assert dry.total_rewrapped == 0

    report = await rotate_keys(owner_sessions, Base.metadata, org_ids=[a], batch_size=2)
    assert report.rewrapped == {"model_calls.request": 3, "model_calls.response": 3}
    assert report.failed == {"model_calls.request": 1}
    rows = await _raw(app_engine, a, "SELECT id, request, response FROM model_calls ORDER BY id")
    for row in rows:
        if row.id == broken:
            assert row.request == "v1:1:AAAA:AAAA:AAAA"
            continue
        assert kek_version(row.request) == "2"
        assert kek_version(row.response) == "2"
    # The old KEK can now go: version 2 alone decrypts every re-wrapped row.
    install_keyring(KeyRing(keks={"2": ring_v2.keks["2"]}, current="2", blind_index_key=b"k" * 32))
    async with _session(app_engine, a) as s, s.begin():
        for i, call_id in enumerate(ids):
            stored = await DatabaseCallStore().get(s, call_id)
            assert stored is not None
            assert stored.response == {"i": i}
    install_keyring(ring_v2)
    # Org B was not asked for and keeps its old wrapping.
    (b_row,) = await _raw(app_engine, b, "SELECT request FROM model_calls WHERE id = :id", id=other)
    assert kek_version(b_row.request) == "1"

    # Resumable: a second run finds only the broken row.
    again = await rotate_keys(owner_sessions, Base.metadata, org_ids=[a], batch_size=2)
    assert again.total_rewrapped == 0
    assert again.failed == {"model_calls.request": 1}

    events = await _raw(
        app_engine,
        a,
        "SELECT payload FROM audit_events WHERE action = :action ORDER BY seq",
        action=REWRAPPED_ACTION,
    )
    assert sum(e.payload["rewrapped"] for e in events) == 6
    assert all(e.payload["kek_version"] == "2" for e in events)

    # The application role cannot enumerate tenants through the rotation helper.
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT * FROM crypto_org_ids()"))


async def _insert_call_raw(conn: Any, org: uuid.UUID, token: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO model_calls (id, org_id, chain_id, attempt, task, prompt_version, "
            "prompt_hash, provider, model, tier, schema_name, schema_hash, request_hash, mode, "
            "status, request) VALUES (:id, :org, gen_random_uuid(), 1, 't', 'v1', "
            "repeat('a', 64), 'p', 'm', 'small', 'S', repeat('a', 64), repeat('b', 64), "
            "'live', 'ok', :token)"
        ),
        {"id": row_id, "org": org, "token": token},
    )
    return row_id


# ------------------------------------------------------------------ data migration


def _alembic(owner_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = owner_url
    return config


async def _seed_plain_rows(owner_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = create_async_engine(owner_url, pool_size=1, max_overflow=0)
    org, call, email = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug) VALUES (:id, 'Mig', :slug)"),
                {"id": org, "slug": f"mig-{org.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO model_calls (id, org_id, chain_id, attempt, task, "
                    "prompt_version, prompt_hash, provider, model, tier, schema_name, "
                    "schema_hash, request_hash, mode, status, request, response) VALUES "
                    "(:id, :org, gen_random_uuid(), 1, 't', 'v1', repeat('a', 64), 'p', 'm', "
                    "'small', 'S', repeat('a', 64), repeat('c', 64), 'live', 'ok', "
                    "CAST(:req AS jsonb), CAST(:resp AS jsonb))"
                ),
                {
                    "id": call,
                    "org": org,
                    "req": '{"inputs": {"text": "plain before 0008"}, "settings": {}}',
                    "resp": '{"label": "old"}',
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO inbound_emails (id, org_id, workflow, message_id, raw_sha256, "
                    "sender_address) VALUES (:id, :org, 'pay', '<m@example.test>', "
                    "repeat('e', 64), 'Old.Sender@Example.test')"
                ),
                {"id": email, "org": org},
            )
    finally:
        await engine.dispose()
    return org, call, email


async def _check_and_drop(
    owner_url: str, org: uuid.UUID, call: uuid.UUID, email: uuid.UUID
) -> None:
    engine = create_async_engine(owner_url, pool_size=1, max_overflow=0)
    try:
        async with _session(engine, org) as s, s.begin():
            stored = await DatabaseCallStore().get(s, call)
            assert stored is not None
            assert stored.request["inputs"]["text"] == "plain before 0008"
            assert stored.response == {"label": "old"}
            row = (await s.scalars(select(InboundEmail).where(InboundEmail.id == email))).one()
            assert unseal(row, "sender_address") == "Old.Sender@Example.test"
            assert row.sender_address_bidx == blind_index("email", "old.sender@example.test", org)
            raw = (
                await s.execute(
                    text("SELECT request FROM model_calls WHERE id = :id"), {"id": call}
                )
            ).one()
            assert raw.request.startswith("v1:")
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
            await conn.execute(text("DELETE FROM model_calls"))
            await conn.execute(text("DELETE FROM inbound_emails"))
            await conn.execute(text("DELETE FROM organisations WHERE id = :o"), {"o": org})
    finally:
        await engine.dispose()


SCRATCH_OWNER_ENV = "PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER"
_ENCRYPTED_TABLES = ("model_calls", "inbound_emails")


async def _clear_encrypted_rows(owner_url: str) -> None:
    """Remove rows an earlier session left in the scratch database.

    They were encrypted with that session's random keys, which this session does not
    have, so the downgrade below could not decrypt them. The scratch database belongs
    to this test alone, so nothing else loses data.
    """
    engine = create_async_engine(owner_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            for table in _ENCRYPTED_TABLES:
                # RLS is forced on the owner too: lift it for this transaction only.
                await conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608  # nosec B608 (fixed names)
                await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    finally:
        await engine.dispose()


def test_migration_0008_encrypts_existing_rows(
    migrated: DbUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downgrade to 0006, write plaintext rows, upgrade: they come back encrypted.

    Runs in a dedicated scratch database (``PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER``,
    created by ``scripts/ci_postgres.sh``), never in the shared test database: the
    downgrade decrypts every encrypted row with this session's keys, and the shared
    database may hold rows from other tests or from earlier sessions with other keys.
    """
    scratch = os.environ.get(SCRATCH_OWNER_ENV)
    if not scratch:
        pytest.skip(f"the migration round trip needs {SCRATCH_OWNER_ENV} (a dedicated database)")
    owner = to_asyncpg_url(scratch)
    monkeypatch.setenv("PAIRLY_DB_APP_ROLE", make_url(migrated.app).username or "pairly_app")
    config = _alembic(owner)
    command.upgrade(config, "head")
    asyncio.run(_clear_encrypted_rows(owner))
    try:
        command.downgrade(config, "0006")
        org, call, email = asyncio.run(_seed_plain_rows(owner))
        command.upgrade(config, "head")
        asyncio.run(_check_and_drop(owner, org, call, email))
    finally:
        command.upgrade(config, "head")


# ------------------------------------------------------------------ startup


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("PAIRLY_KEK_") or name == "PAIRLY_BLIND_INDEX_KEY":
            monkeypatch.delenv(name)
    previous = keys_mod._keyring
    install_keyring(None)
    yield
    install_keyring(previous)


def test_api_refuses_to_start_without_keys(no_keys: None) -> None:
    from pairly_api.main import create_app

    with pytest.raises(CryptoConfigError, match="PAIRLY_KEK_CURRENT"):
        create_app(make_settings(), plugins=[])


async def test_worker_refuses_to_start_without_keys(no_keys: None) -> None:
    import pairly_worker.main as worker_main
    from pairly_worker.config import load_worker_config

    config = load_worker_config(
        database_url="postgresql://pairly_app:pw@127.0.0.1:1/none",
        redis_url="redis://:pw@127.0.0.1:1/0",
    )
    with pytest.raises(CryptoConfigError, match="PAIRLY_BLIND_INDEX_KEY"):
        await worker_main.startup({"config": config})

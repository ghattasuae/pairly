# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind index key rotation against a real Postgres: dual lookup during the rollout,
the rewrite command (resumable, audited, compare-and-swap) and the admin CLI."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import pairly_core.models  # noqa: F401
from pairly_api import admin
from pairly_core.crypto import keys as keys_mod
from pairly_core.crypto.blind_index import blind_index, blind_index_candidates
from pairly_core.crypto.blind_index_rotation import REWRITTEN_ACTION, rotate_blind_indexes
from pairly_core.crypto.keys import KeyRing, install_keyring, load_keyring
from pairly_core.crypto.types import seal
from pairly_core.db import Base, create_sessionmaker, set_tenant
from pairly_core.ingestion.models import InboundEmail

from .conftest import DbUrls, TwoTenants


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


ENV_V1 = {"PAIRLY_KEK_1": _key(), "PAIRLY_KEK_CURRENT": "1", "PAIRLY_BLIND_INDEX_KEY": _key()}
ENV_BOTH = {**ENV_V1, "PAIRLY_BLIND_INDEX_KEY_2": _key()}
ENV_V2 = {**ENV_BOTH, "PAIRLY_BLIND_INDEX_KEY_CURRENT": "2"}


@pytest.fixture
def restore_keyring() -> Iterator[None]:
    previous = keys_mod._keyring
    yield
    install_keyring(previous)


@pytest.fixture
async def clean_emails(owner_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[TwoTenants]:
    yield tenants
    for tenant in (tenants.a, tenants.b):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(tenant.org_id)}
            )
            await conn.execute(text("DELETE FROM inbound_emails"))


def _session(engine: AsyncEngine, org: uuid.UUID) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, None)
    return session


async def _add(engine: AsyncEngine, org: uuid.UUID, address: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with _session(engine, org) as s, s.begin():
        s.add(
            InboundEmail(
                id=row_id,
                org_id=org,
                workflow="pay",
                message_id=f"<{uuid.uuid4()}@example.test>",
                raw_sha256="e" * 64,
                sender_address=seal(
                    InboundEmail, "sender_address", address, org_id=org, row_id=row_id
                ),
                sender_address_bidx=blind_index("email", address, org),
            )
        )
    return row_id


async def _find(engine: AsyncEngine, org: uuid.UUID, address: str) -> list[uuid.UUID]:
    candidates = blind_index_candidates("email", address, org)
    async with _session(engine, org) as s, s.begin():
        rows = await s.scalars(
            select(InboundEmail.id).where(InboundEmail.sender_address_bidx.in_(candidates))
        )
        return list(rows)


async def _raw(engine: AsyncEngine, org: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with _session(engine, org) as s, s.begin():
        return list((await s.execute(text(sql), params)).all())


async def test_rotation_keeps_every_lookup_working(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    clean_emails: TwoTenants,
    restore_keyring: None,
) -> None:
    a, b = clean_emails.a.org_id, clean_emails.b.org_id
    v1 = load_keyring(ENV_V1)
    install_keyring(v1)
    old = [await _add(app_engine, a, f"vendor{i}@example-supplies.test") for i in range(3)]
    other = await _add(app_engine, b, "vendor0@example-supplies.test")

    # Phase 1: key 2 added, still writing with key 1: lookups try both.
    install_keyring(load_keyring(ENV_BOTH))
    assert await _find(app_engine, a, "Vendor0@Example-Supplies.test") == [old[0]]

    # Phase 2: key 2 current. Old rows are still found, new rows use key 2.
    v2 = load_keyring(ENV_V2)
    install_keyring(v2)
    new = await _add(app_engine, a, "vendor9@example-supplies.test")
    assert await _find(app_engine, a, "vendor1@example-supplies.test") == [old[1]]
    assert await _find(app_engine, a, "vendor9@example-supplies.test") == [new]
    # One row whose source no longer decrypts: counted as failed, left alone.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(a)})
        await conn.execute(
            text("UPDATE inbound_emails SET sender_address = 'v1:1:AAAA:AAAA:AAAA' WHERE id = :id"),
            {"id": old[2]},
        )

    owner_sessions = create_sessionmaker(owner_engine)
    dry = await rotate_blind_indexes(owner_sessions, Base.metadata, org_ids=[a], dry_run=True)
    assert dry.pending == {"inbound_emails.sender_address_bidx": 2}
    assert dry.failed == {"inbound_emails.sender_address_bidx": 1}
    assert dry.total_rewritten == 0

    # Phase 3: rewrite. Batches of one exercise the keyset pagination.
    report = await rotate_blind_indexes(owner_sessions, Base.metadata, org_ids=[a], batch_size=1)
    assert report.rewritten == {"inbound_emails.sender_address_bidx": 2}
    assert report.failed == {"inbound_emails.sender_address_bidx": 1}
    assert report.current == "2"

    # Phase 4: key 1 removed. Every rewritten row is found with key 2 alone.
    only_v2 = KeyRing(
        keks=v2.keks,
        current=v2.current,
        blind_index_key=v2.blind_index_key,
        blind_index_keys={"2": v2.blind_index_key},
        blind_index_current="2",
    )
    install_keyring(only_v2)
    for i in (0, 1):
        assert await _find(app_engine, a, f"vendor{i}@example-supplies.test") == [old[i]]
    assert await _find(app_engine, a, "vendor9@example-supplies.test") == [new]
    install_keyring(v2)
    # Org B was not asked for: still on key 1.
    (b_row,) = await _raw(
        app_engine, b, "SELECT sender_address_bidx FROM inbound_emails WHERE id = :id", id=other
    )
    assert b_row.sender_address_bidx == blind_index(
        "email", "vendor0@example-supplies.test", b, v2, version="1"
    )

    # Resumable and idempotent: nothing left but the broken row.
    again = await rotate_blind_indexes(owner_sessions, Base.metadata, org_ids=[a])
    assert again.total_rewritten == 0
    assert again.failed == {"inbound_emails.sender_address_bidx": 1}
    events = await _raw(
        app_engine,
        a,
        "SELECT payload FROM audit_events WHERE action = :action ORDER BY seq",
        action=REWRITTEN_ACTION,
    )
    assert sum(e.payload["rewritten"] for e in events) == 2
    assert all(e.payload["blind_index_version"] == "2" for e in events)


def test_admin_command(
    migrated: DbUrls,
    restore_keyring: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in ENV_V2.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(admin.OWNER_URL_ENV, migrated.owner)
    org = str(uuid.uuid4())  # an organisation with no rows: nothing to rewrite
    code = admin.main(["crypto", "rotate-blind-index", "--org", org, "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Current blind index key version: 2." in out
    assert "Nothing to rewrite" in out
    assert admin.main(["crypto", "rotate-blind-index", "--org", org]) == 0
    assert "Rewrote 0 blind indexes, 0 failed." in capsys.readouterr().out
    assert admin.main(["crypto", "rotate-blind-index", "--batch-size", "0"]) == 2

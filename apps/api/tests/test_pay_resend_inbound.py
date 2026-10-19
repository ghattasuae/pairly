# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Resend inbound email for Pay (``pairly_pay.resend_inbound``) on Postgres, offline.

The Receiving API is a fake that returns a rendered demo email; everything else is real:
routing by the event's recipients, the ``inbound_events`` record keyed on ``svix-id``, and
Pay's intake. A retried event is a duplicate, an unknown address fetches nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.ingestion.addresses import inbound_address
from pairly_pay.resend_inbound import ReceivedEvent, handle_received

from .hire_support import ingestion_settings
from .notify_support import notify_settings
from .pay_support import PayWorld, owner_rows, retire_documents
from .test_pay_inbound_drop import _demo_email
from .test_pay_invoices import DOMAIN, Api, _address, api, world

__all__ = ["api", "world"]  # fixtures

EMAIL_ID = "56761188-7520-42d8-8898-ff6fc54ce618"


async def test_resend_event_is_ingested_once(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    address = await _address(api, world, owner_engine)
    raw = _demo_email("clean", address)
    fetched: list[str] = []

    async def fetch(email_id: str, limit: int) -> bytes:
        fetched.append(email_id)
        assert limit == ingestion_settings().max_email_bytes
        return raw

    async def run(msg_id: str, recipients: tuple[str, ...]) -> str:
        return await handle_received(
            ReceivedEvent(msg_id, EMAIL_ID, recipients),
            sessionmaker=create_sessionmaker(api.engine),
            store=api.store,
            ingestion=ingestion_settings(),
            notifications=notify_settings(),
            fetch_raw=fetch,
        )

    assert await run("msg_first", (address,)) == "ingested"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT source FROM pay_invoices WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert [r.source for r in rows] == ["email"]
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT provider, event_id FROM inbound_events WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert [(e.provider, e.event_id) for e in events] == [("resend", "msg_first")]

    # Resend retries the same event (same svix-id): nothing runs twice.
    assert await run("msg_first", (address,)) == "duplicate"
    # A second event for the same message (another svix-id): intake sees the Message-ID.
    assert await run("msg_other", (address,)) == "duplicate"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM pay_invoices WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert rows[0].n == 1

    # A wrong token is dropped before anything is fetched.
    fetched.clear()
    wrong = inbound_address("pay", world.slug, "a" * 16, DOMAIN)
    assert await run("msg_wrong", (wrong,)) == "unroutable"
    assert fetched == []
    await retire_documents(owner_engine, world.org_id)


async def test_failed_intake_records_nothing(
    api: Api, world: PayWorld, owner_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = await _address(api, world, owner_engine)

    async def broken(email_id: str, limit: int) -> bytes:
        return b"x" * 10

    async def failing(*args: object, **kwargs: object) -> object:
        raise RuntimeError("storage down")

    monkeypatch.setattr("pairly_pay.resend_inbound.receive_email", failing)
    with pytest.raises(RuntimeError, match="storage down"):
        await handle_received(
            ReceivedEvent("msg_fail", EMAIL_ID, (address,)),
            sessionmaker=create_sessionmaker(api.engine),
            store=api.store,
            ingestion=ingestion_settings(),
            notifications=notify_settings(),
            fetch_raw=broken,
        )
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT event_id FROM inbound_events WHERE org_id = :o AND event_id = 'msg_fail'",
        {"o": world.org_id},
    )
    # Rolled back with the failed intake: Resend's retry will be processed.
    assert events == []

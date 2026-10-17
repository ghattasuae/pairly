# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The development inbound drop (``pairly_pay.inbound_drop``) on Postgres, offline.

* A dropped ``.eml`` reaches the organisation whose inbound address is in its ``To``
  header and becomes its invoice; a wrong token is dropped, not bounced.
* The folder is drained once: delivered files go to ``done/``, files that raised to
  ``failed/``; symbolic links, other suffixes and subfolders are never read.
* Refused outside development and test: the functions, the job and the command line.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker
from pairly_core.ingestion.addresses import inbound_address
from pairly_core.jobs.registry import JobContext
from pairly_pay import inbound_drop
from pairly_pay.inbound_drop import (
    DROP_DIR_ENV,
    DropRefusedError,
    DropServices,
    allowed,
    deliver,
    drain,
    run_drop_job,
)
from synth.pay.emails import render_email

from .hire_support import ingestion_settings
from .notify_support import notify_settings
from .pay_support import PayWorld, owner_rows, retire_documents
from .test_pay_invoices import DOMAIN, Api, _address, api, blob, dataset, world

__all__ = ["api", "world"]  # fixtures


def _services(api: Api) -> DropServices:
    return DropServices(
        sessionmaker=create_sessionmaker(api.engine),
        store=api.store,
        ingestion=ingestion_settings(),
        notifications=notify_settings(),
    )


def _demo_email(kind: str, to: str) -> bytes:
    ds = dataset()
    record = next(e for e in ds.emails if e.demo == kind)
    record = record.model_copy(update={"to": to})
    return render_email(record, ds, {i: blob(i) for i in record.invoice_ids})


async def test_drop_routes_by_the_to_header_and_drains_the_folder(
    api: Api, world: PayWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    address = await _address(api, world, owner_engine)
    folder = tmp_path / "inbound"
    folder.mkdir()
    (folder / "1-clean.eml").write_bytes(_demo_email("clean", address))
    wrong = inbound_address("pay", world.slug, "a" * 16, DOMAIN)
    (folder / "2-wrong.eml").write_bytes(_demo_email("near_duplicate", wrong))
    (folder / "notes.txt").write_text("not an email")
    (folder / "sub").mkdir()
    (folder / "sub" / "x.eml").write_bytes(_demo_email("bank_change", address))
    (folder / "3-link.eml").symlink_to(folder / "1-clean.eml")

    totals = await drain(folder, _services(api))
    assert totals == {"files": 2, "delivered": 1, "unroutable": 1, "failed": 0}
    assert sorted(p.name for p in (folder / "done").iterdir()) == ["1-clean.eml", "2-wrong.eml"]
    assert (folder / "notes.txt").exists()
    assert (folder / "sub" / "x.eml").exists()
    assert (folder / "3-link.eml").is_symlink()
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT source, intake_reference FROM pay_invoices WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert [r.source for r in rows] == ["email"]
    assert rows[0].intake_reference.startswith("REF-")

    # Delivered again by hand: the engine sees a duplicate, nothing new.
    again = await deliver(folder / "done" / "1-clean.eml", _services(api))
    assert again.status == "duplicate"
    assert again.invoice_ids == []
    # A file that is not an .eml or is a link is refused before it is read.
    with pytest.raises(ValueError, match="not an"):
        await deliver(folder / "notes.txt", _services(api))
    with pytest.raises(ValueError, match="not a regular file"):
        await deliver(folder / "3-link.eml", _services(api))
    await retire_documents(owner_engine, world.org_id)


async def test_a_file_that_raises_goes_to_failed(api: Api, tmp_path: Path) -> None:
    folder = tmp_path / "inbound"
    folder.mkdir()
    (folder / "big.eml").write_bytes(b"x" * 2048)
    services = dataclasses.replace(
        _services(api),
        ingestion=ingestion_settings().model_copy(update={"max_email_bytes": 1024}),
    )
    totals = await drain(folder, services)
    assert totals == {"files": 1, "delivered": 0, "unroutable": 0, "failed": 1}
    assert (folder / "failed" / "big.eml").exists()


async def test_refused_outside_development_and_test(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert allowed("development")
    assert allowed(Environment.TEST)
    for env in ("production", "staging", "", None, "dev"):
        assert not allowed(env)
    folder = tmp_path / "inbound"
    folder.mkdir()
    (folder / "a.eml").write_bytes(b"From: a@b.example\r\n\r\nhi\r\n")
    production = dataclasses.replace(
        _services(api),
        ingestion=ingestion_settings().model_copy(update={"env": Environment.PRODUCTION}),
    )
    with pytest.raises(DropRefusedError):
        await drain(folder, production)
    with pytest.raises(DropRefusedError):
        await deliver(folder / "a.eml", production)
    assert (folder / "a.eml").exists()  # untouched

    ctx = JobContext(create_sessionmaker(api.engine), api.runtime, api.runtime.registry)
    monkeypatch.delenv(DROP_DIR_ENV, raising=False)
    assert await run_drop_job(ctx) == {"skipped": "no_drop_dir"}
    monkeypatch.setenv(DROP_DIR_ENV, str(folder))
    monkeypatch.setenv("PAIRLY_ENV", "production")
    assert await run_drop_job(ctx) == {"skipped": "not_development"}
    assert (folder / "a.eml").exists()

    monkeypatch.setenv("PAIRLY_ENV", "staging")
    assert inbound_drop.main([str(folder / "a.eml")]) == 2
    monkeypatch.delenv("PAIRLY_ENV")
    assert "PAIRLY_ENV" not in os.environ
    assert inbound_drop.main([str(folder / "a.eml")]) == 2

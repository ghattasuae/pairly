# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The development inbound drop: ``.eml`` files in, as if mailed to Pay's address.

There is no mail receiver in development, so the demo step "forward three invoices to
the inbound address" goes through files instead. Two ways in, both refused outside
``PAIRLY_ENV=development`` and ``test`` (fail closed: an unset or other environment is
refused):

* ``make inbound-drop FILE=<eml>`` runs ``python -m pairly_pay.inbound_drop FILE...``
  with the worker's environment and delivers each file at once;
* the worker job ``pay_inbound_drop`` polls ``PAIRLY_INBOUND_DROP_DIR`` (the native
  stack sets it to ``.local/inbound/``) every minute and delivers each ``*.eml`` file
  there, oldest first, then moves it to ``done/`` (or ``failed/`` when delivery raised).
  Without the variable the job does nothing.

Each message goes to ``pairly_pay.intake.receive_email`` exactly as a received email
would: the engine routes it by the inbound address in its ``To`` header (an unknown or
rotated token is dropped, never bounced), stores and scans the attachments, and Pay
creates the invoices; the intake job then starts reading them. Only regular files
directly in the folder are read (no symbolic links, nothing in subfolders), and a file
over the engine's email size limit is refused before it is read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pairly_core.config import ConfigError, Environment, to_asyncpg_url
from pairly_core.db import create_sessionmaker
from pairly_core.ingestion.settings import IngestionSettings, load_ingestion_settings
from pairly_core.ingestion.storage import ObjectStore, object_store_from_settings
from pairly_core.jobs.registry import JobContext, JobSpec
from pairly_core.notifications.settings import NotificationSettings, load_notification_settings
from pairly_pay.intake import EmailIntake, receive_email

log = structlog.get_logger("pairly_pay.inbound_drop")

JOB_NAME: Final = "pay_inbound_drop"
DROP_DIR_ENV: Final = "PAIRLY_INBOUND_DROP_DIR"
ALLOWED_ENVS: Final = frozenset({Environment.DEVELOPMENT, Environment.TEST})
SUFFIX: Final = ".eml"
DONE: Final = "done"
FAILED: Final = "failed"
BATCH: Final = 20


class DropRefusedError(RuntimeError):
    """The drop is for development and test only."""


@dataclass(frozen=True, slots=True)
class DropServices:
    sessionmaker: async_sessionmaker[AsyncSession]
    store: ObjectStore
    ingestion: IngestionSettings
    notifications: NotificationSettings


def allowed(env: str | Environment | None) -> bool:
    try:
        return Environment(env) in ALLOWED_ENVS if env else False
    except ValueError:
        return False


def require_allowed(env: str | Environment | None) -> None:
    if not allowed(env):
        raise DropRefusedError("the inbound drop only runs in development and test")


def outcome_json(path: Path, outcome: EmailIntake) -> dict[str, Any]:
    return {
        "file": path.name,
        "status": outcome.status,
        "org_id": str(outcome.org_id) if outcome.org_id else None,
        "classification": outcome.classification,
        "reference": outcome.reference,
        "invoice_ids": [str(i) for i in outcome.invoice_ids],
        "known_invoice_ids": [str(i) for i in outcome.known_invoice_ids],
    }


def read_message(path: Path, limit: int) -> bytes:
    """The file's bytes: a regular ``.eml`` file (no symbolic link) within ``limit``."""
    if path.suffix.lower() != SUFFIX:
        raise ValueError(f"{path.name}: not an {SUFFIX} file")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name}: not a regular file")
    if path.stat().st_size > limit:
        raise ValueError(f"{path.name}: larger than the email size limit")
    return path.read_bytes()


async def deliver(path: Path, services: DropServices) -> EmailIntake:
    """One ``.eml`` file through Pay's email intake, routed by its ``To`` header."""
    require_allowed(services.ingestion.env)
    raw = read_message(path, services.ingestion.max_email_bytes)
    outcome = await receive_email(
        raw,
        sessionmaker=services.sessionmaker,
        store=services.store,
        settings=services.ingestion,
        notification_settings=services.notifications,
    )
    log.info(
        "pay_inbound_drop_delivered",
        file=path.name,
        status=outcome.status,
        invoices=len(outcome.invoice_ids),
    )
    return outcome


def pending(folder: Path) -> list[Path]:
    """The ``.eml`` files waiting directly in ``folder``, oldest first."""
    if not folder.is_dir():
        return []
    files = [
        p
        for p in folder.iterdir()
        if p.suffix.lower() == SUFFIX and p.is_file() and not p.is_symlink()
    ]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))[:BATCH]


def _move(path: Path, sub: str) -> None:
    target = path.parent / sub
    target.mkdir(exist_ok=True)
    path.replace(target / path.name)


async def drain(folder: Path, services: DropServices) -> dict[str, int]:
    """Deliver every waiting file once; each one ends in ``done/`` or ``failed/``."""
    require_allowed(services.ingestion.env)
    totals = {"files": 0, "delivered": 0, "unroutable": 0, "failed": 0}
    for path in pending(folder):
        totals["files"] += 1
        try:
            outcome = await deliver(path, services)
        except Exception as exc:
            totals["failed"] += 1
            log.warning("pay_inbound_drop_failed", file=path.name, error=type(exc).__name__)
            _move(path, FAILED)
            continue
        totals["delivered" if outcome.org_id is not None else "unroutable"] += 1
        _move(path, DONE)
    return totals


def services_from_env(
    sessionmaker: async_sessionmaker[AsyncSession], store: ObjectStore | None = None
) -> DropServices:
    ingestion = load_ingestion_settings()
    return DropServices(
        sessionmaker=sessionmaker,
        store=store if store is not None else object_store_from_settings(ingestion),
        ingestion=ingestion,
        notifications=load_notification_settings(env=ingestion.env),
    )


async def run_drop_job(ctx: JobContext) -> Mapping[str, Any]:
    """The worker job: nothing unless in development or test with the folder set."""
    folder = os.environ.get(DROP_DIR_ENV, "").strip()
    if not folder:
        return {"skipped": "no_drop_dir"}
    if not allowed(os.environ.get("PAIRLY_ENV")):
        log.warning("pay_inbound_drop_refused", reason="not development or test")
        return {"skipped": "not_development"}
    if not pending(Path(folder)):
        return {"files": 0}
    store = getattr(ctx.runtime.services, "store", None) if ctx.runtime else None
    try:
        services = services_from_env(ctx.sessionmaker, store)
    except ConfigError:
        log.warning("pay_inbound_drop_refused", reason="settings")
        return {"skipped": "settings"}
    return await drain(Path(folder), services)


DROP_JOB: Final = JobSpec(
    JOB_NAME,
    "Development only: deliver the .eml files dropped in the inbound folder to Pay",
    "every minute",
    run=run_drop_job,
    every_minutes=1,
)


async def _main(paths: Sequence[Path]) -> list[dict[str, Any]]:
    from pairly_core.crypto.keys import init_crypto

    url = os.environ.get("PAIRLY_DATABASE_URL", "")
    if not url:
        raise SystemExit("PAIRLY_DATABASE_URL is not set")
    init_crypto()
    engine = create_async_engine(to_asyncpg_url(url), pool_size=2, max_overflow=0)
    try:
        services = services_from_env(create_sessionmaker(engine))
        return [outcome_json(p, await deliver(p, services)) for p in paths]
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pairly_pay.inbound_drop",
        description="Development only: deliver .eml files to Pay's inbound address.",
    )
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if not allowed(os.environ.get("PAIRLY_ENV")):
        print("the inbound drop only runs with PAIRLY_ENV=development or test", file=sys.stderr)
        return 2
    try:
        results = asyncio.run(_main(args.files))
    except (ValueError, DropRefusedError, ConfigError) as exc:
        print(f"inbound drop refused: {exc}", file=sys.stderr)
        return 2
    for result in results:
        print(json.dumps(result))
    return 0 if all(r["org_id"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

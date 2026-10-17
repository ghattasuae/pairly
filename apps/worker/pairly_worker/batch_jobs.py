# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Batch API sweeper (cron, every 5 minutes): ``batch_url_sweep``.

URL items of a batch are fetched by the API process after it answered. If that process
stops (a deploy, a crash), the items stay ``pending`` and the batch never completes. The
sweeper fails each URL item still pending ``STALE_AFTER_MINUTES`` after its batch was
created, with the reason ``timed_out`` (audited), and completes the batch, so
``batch.completed`` is delivered. Why it fails instead of retrying is in
``pairly_core.batches.service``.

Organisations are found through ``batch_stale_org_ids`` (migration 0026, ids only); each
is processed in its own tenant-bound session, and one failing organisation never stops
the others.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.batches.service import STALE_AFTER_MINUTES, fail_stale_url_items
from pairly_core.db import set_tenant
from pairly_core.jobs.registry import BATCH_URL_SWEEP

log = structlog.get_logger("pairly_worker.batch_jobs")

BATCH_SWEEP_JOB: Final = BATCH_URL_SWEEP


async def _stale_orgs(
    sessionmaker: async_sessionmaker[AsyncSession], minutes: int
) -> list[uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM batch_stale_org_ids(:m) AS org_id").bindparams(
                bindparam("m", minutes)
            )
        )
        return [uuid.UUID(str(value)) for value in rows.scalars()]


async def sweep_batch_urls(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    minutes: int = int(ctx.get("batch_stale_minutes", STALE_AFTER_MINUTES))
    items = batches = completed = 0
    errors: list[str] = []
    for org_id in await _stale_orgs(sessionmaker, minutes):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    result = await fail_stale_url_items(session, older_than_minutes=minutes)
        except Exception as exc:
            errors.append(str(org_id))
            log.error("batch_url_sweep_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        items += result.items
        batches += result.batches
        completed += result.completed
    summary = {"items": items, "batches": batches, "completed": completed, "errors": errors}
    if items or errors:
        log.info("batch_url_sweep_finished", **summary)
    return summary

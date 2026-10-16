# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention job (brief 6.9).

``retention_run`` (cron, daily): for every organisation, lists what each retention rule
makes due (the seven-day preview list and a report row per rule), then deletes what
the organisation's settings say to delete: technical rows due for a whole preview, and
listed subjects whose preview ended, up to the daily cap, never one under a legal hold
(``pairly_core.retention.service``). The stored files of erased subjects go through the
worker's object store.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.retention.rules import RetentionRegistry
from pairly_core.retention.service import run_retention

log = structlog.get_logger("pairly_worker.retention_jobs")

REGISTRY = "retention_registry"


async def retention_run(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    registry = ctx.get(REGISTRY)
    if not isinstance(registry, RetentionRegistry):
        registry = RetentionRegistry()
    runtime = ctx.get("runtime")
    services = getattr(runtime, "services", None)
    store = getattr(services, "store", None)
    summary = await run_retention(sessionmaker, registry, store=store)
    result = summary.as_dict()
    log.info("retention_run_finished", **result)
    return result

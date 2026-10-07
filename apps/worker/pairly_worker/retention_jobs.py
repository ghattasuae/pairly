# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention dry-run job (WP1.12).

``retention_dry_run`` (cron, daily): for every organisation, counts what each retention
rule would delete and records it in ``retention_reports``
(``pairly_core.retention.service``). It never deletes anything.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.retention.rules import RetentionRegistry
from pairly_core.retention.service import run_dry_run

log = structlog.get_logger("pairly_worker.retention_jobs")

REGISTRY = "retention_registry"


async def retention_dry_run(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    registry = ctx.get(REGISTRY)
    if not isinstance(registry, RetentionRegistry):
        registry = RetentionRegistry()
    summary = await run_dry_run(sessionmaker, registry)
    result = summary.as_dict()
    log.info("retention_dry_run_finished", **result)
    return result

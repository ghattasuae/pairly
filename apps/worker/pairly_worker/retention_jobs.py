# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention job (brief 6.9).

``retention_run`` (cron, daily): for every organisation, lists what each retention rule
makes due (the seven-day preview list and a report row per rule), then deletes what
the organisation's settings say to delete: technical rows due for a whole preview, and
listed subjects whose preview ended, up to the daily cap, never one under a legal hold
(``pairly_core.retention.service``). The stored files of erased subjects go through the
worker's object store. Before a subjects rule's first deletion its admins get an urgent
notice (``pairly_core.retention.notice``) with the worker's role registry and web address.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.rbac import RoleRegistry
from pairly_core.retention.notice import AdminRetentionNotifier, default_notifier
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
    roles = ctx.get("registry")
    transports = ctx.get("transports")
    settings = getattr(transports, "settings", None)
    notifier = (
        AdminRetentionNotifier(registry=roles, web_url=settings.web_url)
        if isinstance(roles, RoleRegistry) and settings is not None
        else default_notifier()
    )
    summary = await run_retention(sessionmaker, registry, store=store, notifier=notifier)
    result = summary.as_dict()
    log.info("retention_run_finished", **result)
    return result

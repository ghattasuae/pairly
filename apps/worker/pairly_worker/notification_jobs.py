# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notification jobs (WP1.9).

* ``deliver_notifications`` (cron, every minute): sends due email and Slack messages,
  with retries and exponential backoff (``pairly_core.notifications.delivery``).
* ``send_digests`` (cron, daily at ``PAIRLY_DIGEST_HOUR_UTC``): one digest email per
  person with held items, then delivered by the next ``deliver_notifications`` run.

Organisations are found through ``notification_due_org_ids()`` (SECURITY DEFINER,
migration 0011), which returns only organisation ids; each organisation is processed in
its own tenant-bound session. One failing organisation never stops the others.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import set_tenant
from pairly_core.notifications.delivery import Transports, build_digests, deliver_org

log = structlog.get_logger("pairly_worker.notification_jobs")


async def _due_org_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM notification_due_org_ids() AS org_id")
        )
        return [uuid.UUID(str(value)) for value in rows.scalars()]


async def deliver_notifications(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    transports: Transports = ctx["transports"]
    totals = {"sent": 0, "retried": 0, "failed": 0}
    errors: list[str] = []
    for org_id in await _due_org_ids(sessionmaker):
        try:
            stats = await deliver_org(sessionmaker, org_id, transports)
        except Exception as exc:
            errors.append(str(org_id))
            log.error(
                "notification_delivery_job_failed", org_id=str(org_id), error=type(exc).__name__
            )
            continue
        totals["sent"] += stats.sent
        totals["retried"] += stats.retried
        totals["failed"] += stats.failed
    summary: dict[str, Any] = {**totals, "errors": errors}
    if any(totals.values()) or errors:
        log.info("notification_delivery_finished", **summary)
    return summary


async def send_digests(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    digests = items = 0
    errors: list[str] = []
    for org_id in await _due_org_ids(sessionmaker):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    stats = await build_digests(session)
        except Exception as exc:
            errors.append(str(org_id))
            log.error("digest_job_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        digests += stats.digests
        items += stats.items
    summary: dict[str, Any] = {"digests": digests, "items": items, "errors": errors}
    log.info("digests_finished", **summary)
    return summary

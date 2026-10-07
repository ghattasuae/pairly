# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention dry run: count, per organisation and rule, what would be deleted.

Nothing is deleted here, ever: the queries are ``SELECT count(*)`` in the organisation's
own tenant session (RLS applies), and the only write is one ``retention_reports`` row per
rule, which a check constraint keeps marked as a dry run. Turning a rule into real
deletion is a later, separate decision (it needs the agreed retention periods).

``run_dry_run`` walks every organisation (``retention_org_ids()``, SECURITY DEFINER, ids
only); one failing organisation never stops the others.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.retention.models import RetentionReport
from pairly_core.retention.rules import RetentionRegistry

log = structlog.get_logger("pairly_core.retention")


@dataclass(frozen=True, slots=True)
class RuleCount:
    rule: str
    cutoff: datetime
    would_delete: int


@dataclass
class DryRunSummary:
    run_id: uuid.UUID
    orgs: int = 0
    errors: int = 0
    totals: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "dry_run": True,
            "orgs": self.orgs,
            "errors": self.errors,
            "would_delete": dict(sorted(self.totals.items())),
        }


async def dry_run_org(
    session: AsyncSession, registry: RetentionRegistry, *, run_id: uuid.UUID
) -> list[RuleCount]:
    """Count each rule's due rows for the session's organisation and record a report."""
    org = current_org(session)
    if org is None:
        raise TenantError("a retention dry run needs a tenant context")
    now: datetime | None = await session.scalar(select(func.now()))
    if now is None:  # pragma: no cover - now() always returns a value
        raise RuntimeError("the database returned no time")
    counts: list[RuleCount] = []
    for rule in registry.rules:
        count = await session.scalar(
            select(func.count())
            .select_from(rule.model)
            # RLS limits the count to the organisation; the filter says so explicitly.
            .where(rule.model.org_id == org, *rule.due(now))
        )
        counts.append(RuleCount(rule.key, rule.cutoff(now), int(count or 0)))
    session.add_all(
        RetentionReport(
            org_id=org,
            run_id=run_id,
            rule=c.rule,
            cutoff=c.cutoff,
            would_delete=c.would_delete,
            dry_run=True,
        )
        for c in counts
    )
    await session.flush()
    return counts


async def retention_org_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM retention_org_ids() AS org_id"))
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def run_dry_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RetentionRegistry,
    *,
    run_id: uuid.UUID | None = None,
) -> DryRunSummary:
    summary = DryRunSummary(run_id=run_id or uuid.uuid4())
    for org_id in await retention_org_ids(sessionmaker):
        summary.orgs += 1
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    counts = await dry_run_org(session, registry, run_id=summary.run_id)
        except Exception as exc:
            summary.errors += 1
            log.error("retention_dry_run_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        for c in counts:
            summary.totals[c.rule] = summary.totals.get(c.rule, 0) + c.would_delete
    return summary


async def latest_report(session: AsyncSession) -> list[RetentionReport]:
    """The rows of the organisation's most recent dry run (empty before the first)."""
    latest = await session.scalar(
        select(RetentionReport.run_id)
        .order_by(RetentionReport.created_at.desc(), RetentionReport.id)
        .limit(1)
    )
    if latest is None:
        return []
    return list(
        await session.scalars(
            select(RetentionReport)
            .where(RetentionReport.run_id == latest)
            .order_by(RetentionReport.rule)
        )
    )

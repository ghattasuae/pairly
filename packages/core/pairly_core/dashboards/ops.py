# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The platform operations dashboard (brief 5.1 item 15), for whoever runs Pairly.

Cross-tenant, numbers only: it never shows tenant content (no names of people, files,
vendors or candidates, no URLs, no payloads). Organisations appear by id, slug and
sandbox flag with counts.

* **Service health**: the API (this process), the database (a ``SELECT 1`` and its
  time), the queue (Redis answers), the worker (its arq heartbeat) and the object store
  (a metadata request for a key that does not exist).
* **Queue and jobs**: queue depth; the latest run of every declared job and the
  failures among the recent runs (``job_runs``, also ``GET /v1/ops/jobs``).
* **Webhooks**: deliveries by status, retried ones, the oldest pending, failures of the
  last 24 hours (``ops_webhook_summary``).
* **Reconciliation**: the summaries (numbers only) of the latest runs of the jobs that
  reconcile (their name has ``reconcile`` or ``sweep`` in it).
* **Errors and latency**: from OpenTelemetry metrics when they are exported, else from
  the API's in-memory request counters; ``source`` says which.
* **Storage and per-tenant usage**: documents, stored bytes, members, model calls (paid
  and reused apart) and cost this month per organisation (``ops_tenant_usage``).
* **Schedules**: the job registry (what runs when).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.dashboards.model import Board, Dashboard, Item, Link, Section, Stat, Unit
from pairly_core.jobs.registry import JobRegistry
from pairly_core.jobs.runs import STATUS_FAILED, JobRun, recent_runs

MICROS: Final = 1_000_000
RECENT_RUNS: Final = 200
RECONCILING: Final = ("reconcile", "sweep")


@dataclass(frozen=True, slots=True)
class Probe:
    """One dependency check: ``ok``, ``down`` or ``not_configured``."""

    name: str
    status: str
    latency_ms: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RequestStats:
    source: str
    since: datetime | None
    requests: int | None
    errors_5xx: int | None
    error_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


@dataclass(frozen=True, slots=True)
class OpsInputs:
    probes: Sequence[Probe]
    queue_depth: int | None
    requests: RequestStats
    jobs: JobRegistry


def _health(inputs: OpsInputs) -> Section:
    items = [
        Item(
            kind="service",
            id=p.name,
            label=p.name,
            status=p.status,
            detail={"latency_ms": p.latency_ms, "detail": p.detail},
        )
        for p in inputs.probes
    ]
    down = sum(1 for p in inputs.probes if p.status == "down")
    return Section(key="service_health", count=down, items=items)


def _jobs(runs: Sequence[JobRun], registry: JobRegistry, depth: int | None) -> Section:
    latest: dict[str, JobRun] = {}
    failures: dict[str, int] = defaultdict(int)
    for run in runs:  # newest first
        latest.setdefault(run.job_name, run)
        if run.status == STATUS_FAILED:
            failures[run.job_name] += 1
    items = []
    for spec in registry.jobs:
        last = latest.get(spec.name)
        items.append(
            Item(
                kind="job",
                id=spec.name,
                label=spec.name,
                href=f"/ops/jobs?job={spec.name}",
                at=last.started_at if last else None,
                status=last.status if last else "never_ran",
                detail={
                    "failures_recent": failures.get(spec.name, 0),
                    "error": last.error if last else None,
                    "finished_at": (
                        last.finished_at.isoformat() if last and last.finished_at else None
                    ),
                },
            )
        )
    failed = sum(failures.values())
    return Section(
        key="jobs",
        count=failed,
        href="/ops/jobs",
        items=sorted(items, key=lambda i: (-(int(i.detail["failures_recent"] or 0)), i.id)),
        stats=[
            Stat("queue_depth", float(depth) if depth is not None else None),
            Stat("job_failures_recent", float(failed), n=len(runs), href="/ops/jobs"),
        ],
        source="job_runs",
    )


def _reconciliation(runs: Sequence[JobRun]) -> Section:
    latest: dict[str, JobRun] = {}
    for run in runs:
        if any(word in run.job_name for word in RECONCILING):
            latest.setdefault(run.job_name, run)
    items = []
    for name, run in sorted(latest.items()):
        summary: dict[str, Any] = run.summary or {}
        detail: dict[str, str | int | float | bool | None] = {
            str(k)[:40]: v
            for k, v in list(summary.items())[:8]
            if v is None or isinstance(v, bool | int | float)
        }
        items.append(
            Item(
                kind="job_summary",
                id=name,
                label=name,
                at=run.started_at,
                status=run.status,
                detail=detail,
            )
        )
    return Section(
        key="reconciliation",
        count=len(items),
        href="/ops/jobs",
        items=items,
        source="job_runs",
        note=None if items else "no_data",
    )


async def _webhooks(session: AsyncSession) -> Section:
    rows = (await session.execute(text("SELECT * FROM ops_webhook_summary()"))).all()
    by_status = {r.status: r for r in rows}
    pending = by_status.get("pending")
    stats = [Stat(f"deliveries_{r.status}", float(r.deliveries)) for r in rows] + [
        Stat("deliveries_retried", float(sum(int(r.retried) for r in rows))),
        Stat("deliveries_failed_24h", float(sum(int(r.failed_24h) for r in rows))),
    ]
    oldest = pending.oldest_pending if pending is not None else None
    return Section(
        key="webhooks",
        count=int(pending.deliveries) if pending is not None else 0,
        stats=stats,
        items=(
            [Item(kind="oldest_pending", id="pending", label="pending", at=oldest)]
            if oldest is not None
            else []
        ),
        source="webhook_deliveries",
        note=None if rows else "no_data",
    )


def _requests(stats: RequestStats) -> Section:
    return Section(
        key="requests",
        count=stats.requests,
        stats=[
            Stat("requests", float(stats.requests) if stats.requests is not None else None),
            Stat("error_rate_5xx", stats.error_rate, Unit.RATIO, n=stats.requests),
            Stat("latency_p50", stats.p50_ms, Unit.MS),
            Stat("latency_p95", stats.p95_ms, Unit.MS),
            Stat("latency_p99", stats.p99_ms, Unit.MS),
        ],
        items=(
            [Item(kind="since", id="since", label="since", at=stats.since)]
            if stats.since is not None
            else []
        ),
        source=stats.source,
    )


async def _tenants(session: AsyncSession, now: datetime) -> tuple[Section, Section]:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await session.execute(
            text("SELECT * FROM ops_tenant_usage(:since)").bindparams(
                bindparam("since", start, type_=TIMESTAMP(timezone=True))
            )
        )
    ).all()
    stored = sum(int(r.storage_bytes) for r in rows)
    documents = sum(int(r.documents) for r in rows)
    storage = Section(
        key="storage",
        count=documents,
        stats=[
            Stat("storage_bytes", float(stored), Unit.BYTES),
            Stat("documents", float(documents)),
        ],
        source="documents_size_bytes",
    )
    usage = Section(
        key="tenant_usage",
        count=len(rows),
        items=[
            Item(
                kind="tenant",
                id=str(r.org_id),
                label=str(r.org_slug),
                detail={
                    "sandbox": bool(r.sandbox),
                    "members": int(r.members),
                    "documents": int(r.documents),
                    "storage_bytes": int(r.storage_bytes),
                    # Every call, then apart: the paid ones and those that reused an
                    # earlier answer (no model request, zero cost), as on the AI
                    # governance board.
                    "model_calls_month": int(r.model_calls),
                    "paid_calls_month": int(r.model_calls) - int(r.reused_calls),
                    "reused_calls_month": int(r.reused_calls),
                    "cost_usd_month": int(r.cost_micros) / MICROS,
                },
            )
            for r in sorted(rows, key=lambda r: (-int(r.cost_micros), -int(r.documents)))[:50]
        ],
        source="ops_tenant_usage",
    )
    return storage, usage


def _schedules(registry: JobRegistry) -> Section:
    return Section(
        key="schedules",
        count=len(registry.jobs),
        items=[
            Item(
                kind="schedule",
                id=j.name,
                label=j.name,
                detail={"schedule": j.schedule, "description": j.description},
            )
            for j in registry.jobs
        ],
        source="job_registry",
    )


async def ops_dashboard(session: AsyncSession, *, now: datetime, inputs: OpsInputs) -> Dashboard:
    runs = await recent_runs(session, limit=RECENT_RUNS)
    health = _health(inputs)
    jobs = _jobs(runs, inputs.jobs, inputs.queue_depth)
    webhooks = await _webhooks(session)
    storage, usage = await _tenants(session, now)
    action: list[Section] = []
    if health.count:
        action.append(
            Section(
                key="services_down",
                count=health.count,
                items=[i for i in health.items if i.status == "down"],
            )
        )
    if jobs.count:
        action.append(
            Section(
                key="job_failures",
                count=jobs.count,
                href=jobs.href,
                items=[i for i in jobs.items if i.detail.get("failures_recent")],
            )
        )
    return Dashboard(
        board=Board.PLATFORM_OPS,
        generated_at=now,
        action=action,
        sections=[
            health,
            jobs,
            webhooks,
            _reconciliation(runs),
            _requests(inputs.requests),
            storage,
            usage,
            _schedules(inputs.jobs),
        ],
        links=[Link("jobs", "/ops/jobs")],
    )

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Platform operations (brief 5.1 item 15), for whoever runs this deployment.

* ``GET /v1/ops/dashboard``  service health, queue and jobs, webhooks, reconciliation,
  errors and latency, storage, per-tenant usage, schedules (``pairly_core.dashboards.ops``)
* ``GET /v1/ops/jobs``       recent ``job_runs`` (``recent_runs``), optionally one job

Who: a person who is an admin (``core.platform.read``) of the deployment's system
organisation, ``PAIRLY_SYSTEM_ORG_ID``. Anyone else, an admin of any other organisation
and any API key included, gets 404, as when the setting is unset: the routes do not
exist for them. This is simpler than a ``platform_operator`` role: a role lives inside
an organisation, and the granting rules would have to stop every tenant admin from
handing out a cross-tenant power; a setting only whoever runs the deployment can change
has no such hole.

Everything returned is numbers, statuses, job names and organisation ids and slugs:
never tenant content.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pairly_api import __version__
from pairly_api.request_metrics import RequestMetrics
from pairly_api.routes.dashboards import SysDashboardOut, dashboard_out
from pairly_core.dashboards.ops import OpsInputs, Probe, RequestStats, ops_dashboard
from pairly_core.dashboards.permissions import PLATFORM_READ
from pairly_core.db import ping
from pairly_core.errors import NotFoundError
from pairly_core.identity import Context, OrgSession
from pairly_core.ingestion.storage import blob_key
from pairly_core.jobs.registry import JOB_NAME_RE, JobRegistry
from pairly_core.jobs.runs import recent_runs
from pairly_core.rbac import Actor, ActorKind, require

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])

# A key that never exists: the object store probe only asks for its metadata.
_PROBE_KEY = blob_key(uuid.UUID(int=0), "0" * 64)


def platform_actor(actor: Annotated[Actor, Depends(require(PLATFORM_READ))], ctx: Context) -> Actor:
    system = ctx.settings.system_org_id
    # People only: an API key (even one with this scope) never reads across tenants.
    if system is None or actor.org_id != system or actor.kind is not ActorKind.USER:
        raise NotFoundError("not found")
    return actor


Operator = Annotated[Actor, Depends(platform_actor)]


class OpsJobRunOut(BaseModel):
    id: uuid.UUID
    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: dict[str, Any] | None
    error: str | None


class OpsJobsOut(BaseModel):
    items: list[OpsJobRunOut]


async def _timed(name: str, check: Any) -> Probe:
    started = time.perf_counter()
    try:
        await check()
    except Exception as exc:
        return Probe(name, "down", detail=type(exc).__name__)
    return Probe(name, "ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))


async def _probes(request: Request, ctx: Any) -> tuple[list[Probe], int | None]:
    probes = [Probe("api", "ok", detail=__version__)]
    probes.append(await _timed("database", lambda: ping(ctx.engine)))
    documents = getattr(request.app.state, "documents", None)
    queue = getattr(documents, "queue", None)
    health = getattr(queue, "health", None)
    depth: int | None = None
    if health is None:
        probes.append(Probe("queue", "not_configured"))
        probes.append(Probe("worker", "not_configured"))
    else:
        started = time.perf_counter()
        try:
            status = await health()
        except Exception as exc:
            probes.append(Probe("queue", "down", detail=type(exc).__name__))
            probes.append(Probe("worker", "down", detail="queue_unreachable"))
        else:
            depth = status.depth
            probes.append(
                Probe("queue", "ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))
            )
            beat = status.worker_heartbeat
            probes.append(
                Probe("worker", "ok", detail=beat)
                if beat
                else Probe("worker", "down", detail="no_heartbeat")
            )
    store = getattr(documents, "store", None)
    if store is None:
        probes.append(Probe("object_store", "not_configured"))
    else:
        probes.append(await _timed("object_store", lambda: store.exists(_PROBE_KEY)))
    return probes, depth


def _requests(request: Request) -> RequestStats:
    metrics = getattr(request.app.state, "request_metrics", None)
    if not isinstance(metrics, RequestMetrics):
        return RequestStats("none", None, None, None, None, None, None, None)
    snap = metrics.snapshot()
    return RequestStats(
        source=snap.source,
        since=datetime.fromtimestamp(snap.since, UTC),
        requests=snap.requests,
        errors_5xx=snap.errors_5xx,
        error_rate=snap.error_rate,
        p50_ms=snap.p50_ms,
        p95_ms=snap.p95_ms,
        p99_ms=snap.p99_ms,
    )


@router.get("/dashboard", response_model=SysDashboardOut)
async def dashboard(
    actor: Operator, session: OrgSession, request: Request, ctx: Context
) -> SysDashboardOut:
    probes, depth = await _probes(request, ctx)
    jobs = getattr(request.app.state, "job_registry", None)
    board = await ops_dashboard(
        session,
        now=datetime.now(UTC),
        inputs=OpsInputs(
            probes=probes,
            queue_depth=depth,
            requests=_requests(request),
            jobs=jobs if isinstance(jobs, JobRegistry) else JobRegistry(),
        ),
    )
    return dashboard_out(board)


@router.get("/jobs", response_model=OpsJobsOut)
async def jobs(
    actor: Operator,
    session: OrgSession,
    job_name: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OpsJobsOut:
    if job_name is not None and not JOB_NAME_RE.fullmatch(job_name):
        raise NotFoundError("unknown job")
    runs = await recent_runs(session, job_name=job_name, limit=limit)
    return OpsJobsOut(items=[OpsJobRunOut.model_validate(r, from_attributes=True) for r in runs])

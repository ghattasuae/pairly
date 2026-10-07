# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Queueing the decision and audit worker jobs (arq on Redis).

Enqueueing is best effort, like documents: the job row is committed first, then the
job is queued. A lost enqueue is found by the worker's sweeper (``sweep_decision_jobs``,
through ``decision_jobs_pending_org_ids``), so a job never stalls silently.

Arguments are plain strings (JSON serialised by the shared serializer, never pickle).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

import structlog

from pairly_core.ingestion.queue import job_dumps, job_loads

log = structlog.get_logger(__name__)

COMPLIANCE_EXPORT_JOB: Final = "compliance_export"
VERIFY_AUDIT_CHAIN_JOB: Final = "verify_audit_chain"


@runtime_checkable
class TaskQueue(Protocol):
    async def enqueue(self, job: str, *args: str, job_id: str) -> bool:
        """Queue ``job(*args)``. Returns False when it could not be queued."""
        ...


class ArqTaskQueue:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, redis_url: str) -> ArqTaskQueue:
        from arq.connections import RedisSettings, create_pool

        pool = await create_pool(
            RedisSettings.from_dsn(redis_url),
            job_serializer=job_dumps,
            job_deserializer=job_loads,
        )
        return cls(pool)

    async def enqueue(self, job: str, *args: str, job_id: str) -> bool:
        try:
            queued = await self._pool.enqueue_job(job, *args, _job_id=job_id)
        except Exception as exc:
            log.warning("enqueue_failed", job=job, error=type(exc).__name__)
            return False
        return queued is not None

    async def aclose(self) -> None:
        close = getattr(self._pool, "aclose", None) or getattr(self._pool, "close", None)
        if close is not None:
            await close()


class NullTaskQueue:
    """No queue configured: the worker's sweeper picks the job up."""

    async def enqueue(self, job: str, *args: str, job_id: str) -> bool:
        log.info("enqueue_skipped", job=job)
        return False


@dataclass
class RecordingTaskQueue:
    """Test double: remembers every enqueue."""

    jobs: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)

    async def enqueue(self, job: str, *args: str, job_id: str) -> bool:
        self.jobs.append((job, args, job_id))
        return True

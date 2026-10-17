# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Queueing document jobs (arq on Redis) behind a small interface.

Enqueueing is best effort: the API enqueues the scan after the upload commits, the scan
job enqueues the parse. If an enqueue is lost (Redis down, process killed), the
worker's ``sweep_documents`` cron finds the document by its status and queues it again,
so a document never stalls silently.

Job arguments are plain strings (JSON serialised, never pickle). Job ids include the
attempt so arq's duplicate-id guard drops double enqueues of the same attempt only.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)

SCAN_JOB: Final = "scan_document"
PARSE_JOB: Final = "parse_document"


def job_dumps(value: Any) -> bytes:
    """arq job serializer shared by the API and the worker (JSON, never pickle)."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def job_loads(data: bytes) -> Any:
    return json.loads(data)


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self, job: str, *, org_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> bool:
        """Queue ``job`` for one document. Returns False when it could not be queued."""
        ...


@dataclass(frozen=True, slots=True)
class QueueHealth:
    depth: int
    # The worker's last health-check line (counts only), None when no worker reported
    # within its interval.
    worker_heartbeat: str | None


class ArqJobQueue:
    """Enqueue through an ``arq.connections.ArqRedis`` pool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @property
    def redis(self) -> Any:
        """The arq pool (a ``redis.asyncio.Redis``), shared with the rate limits."""
        return self._pool

    @classmethod
    async def connect(cls, redis_url: str) -> ArqJobQueue:
        from arq.connections import RedisSettings, create_pool

        pool = await create_pool(
            RedisSettings.from_dsn(redis_url),
            job_serializer=job_dumps,
            job_deserializer=job_loads,
        )
        return cls(pool)

    async def enqueue(
        self, job: str, *, org_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> bool:
        try:
            queued = await self._pool.enqueue_job(
                job, str(org_id), str(document_id), _job_id=f"{job}:{document_id}:{key}"
            )
        except Exception as exc:
            log.warning(
                "enqueue_failed", job=job, document_id=str(document_id), error=type(exc).__name__
            )
            return False
        return queued is not None

    async def health(self) -> QueueHealth:
        """Queue depth and whether a worker reported in (arq's health-check key, which
        a live worker refreshes every ``health_check_interval`` seconds with a TTL)."""
        from arq.constants import default_queue_name, health_check_key_suffix

        depth = int(await self._pool.zcard(default_queue_name))
        beat = await self._pool.get(default_queue_name + health_check_key_suffix)
        text = beat.decode("utf-8", "replace") if isinstance(beat, bytes) else beat
        return QueueHealth(depth=depth, worker_heartbeat=str(text)[:200] if text else None)

    async def aclose(self) -> None:
        close = getattr(self._pool, "aclose", None) or getattr(self._pool, "close", None)
        if close is not None:
            await close()


class NullJobQueue:
    """No queue configured: documents are picked up by the worker's sweeper."""

    async def enqueue(
        self, job: str, *, org_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> bool:
        log.info("enqueue_skipped", job=job, document_id=str(document_id))
        return False


@dataclass
class RecordingJobQueue:
    """Test double: remembers every enqueue."""

    jobs: list[tuple[str, uuid.UUID, uuid.UUID, str]] = field(default_factory=list)

    async def enqueue(
        self, job: str, *, org_id: uuid.UUID, document_id: uuid.UUID, key: str
    ) -> bool:
        self.jobs.append((job, org_id, document_id, key))
        return True

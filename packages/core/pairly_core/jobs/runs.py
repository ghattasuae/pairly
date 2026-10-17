# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``job_runs``: the history of scheduled job runs (migration 0013).

The application role has no privilege on the table itself. It records and reads runs
only through three SECURITY DEFINER functions: ``job_run_start(name)``,
``job_run_finish(id, status, summary, error)`` (a finished run never changes again) and
``job_runs_recent(name, limit)``. Job history is platform data: no tenant request can
read or rewrite it. Each job keeps 30 days of history (``job_run_start`` removes older
runs of the same job). A worker that dies mid-run leaves its row ``running``.

``tracked(name, job)`` wraps an arq job coroutine ``job(ctx)``: it records the start,
runs the job, then records ``succeeded`` with the job's summary or ``failed`` with the
exception's class name (never its message, which may hold data), and re-raises. Recording
is best effort: if the database is unreachable the job still runs and the failure to
record is logged.

Summaries keep only what an operator needs: numbers, booleans, short strings, and lists
reduced to their length. They are capped well under the table's 16 KiB limit.
"""

from __future__ import annotations

import functools
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog
from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.jobs.registry import JOB_NAME_RE, JobRegistryError

log = structlog.get_logger("pairly_core.jobs")

MAX_SUMMARY_BYTES: Final = 8 * 1024
MAX_SUMMARY_KEYS: Final = 50
MAX_STRING: Final = 100
STATUS_SUCCEEDED: Final = "succeeded"
STATUS_FAILED: Final = "failed"

JobFn = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class JobRun:
    id: uuid.UUID
    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: dict[str, Any] | None
    error: str | None


def _clean_value(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, list | tuple | set | frozenset):
        return len(value)  # a list of ids is reduced to its count
    if isinstance(value, Mapping) and depth < 2:
        return {
            str(k)[:64]: _clean_value(v, depth + 1)
            for k, v in list(value.items())[:MAX_SUMMARY_KEYS]
        }
    return str(type(value).__name__)


def clean_summary(result: Any) -> dict[str, Any] | None:
    """A small JSON object safe to keep in ``job_runs.summary`` (None if not a mapping)."""
    if not isinstance(result, Mapping):
        return None
    summary = _clean_value(result, 0)
    if not isinstance(summary, dict):  # pragma: no cover - a mapping stays a dict
        return None
    if len(json.dumps(summary, default=str).encode("utf-8")) > MAX_SUMMARY_BYTES:
        return {"truncated": True}
    return summary


async def start_run(sessionmaker: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    if not JOB_NAME_RE.fullmatch(name):
        raise JobRegistryError(f"invalid job name {name!r}")
    async with sessionmaker() as session, session.begin():
        run_id = await session.scalar(
            text("SELECT job_run_start(:n)").bindparams(bindparam("n", name, type_=Text))
        )
    if not isinstance(run_id, uuid.UUID):  # pragma: no cover - the function returns a uuid
        raise RuntimeError("job_run_start returned no id")
    return run_id


async def finish_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> bool:
    """Record the end of ``run_id``. Returns False when it was already finished."""
    async with sessionmaker() as session, session.begin():
        done = await session.scalar(
            text("SELECT job_run_finish(:i, :s, :m, :e)").bindparams(
                bindparam("i", run_id),
                bindparam("s", status, type_=Text),
                # SQL NULL for no summary, never a JSON null (the table wants an object).
                bindparam("m", summary, type_=JSONB(none_as_null=True)),
                bindparam("e", error, type_=Text),
            )
        )
    return bool(done)


async def recent_runs(
    session: AsyncSession, *, job_name: str | None = None, limit: int = 50
) -> list[JobRun]:
    rows = await session.execute(
        text(
            "SELECT id, job_name, started_at, finished_at, status, summary, error "
            "FROM job_runs_recent(:j, :l)"
        ).bindparams(bindparam("j", job_name, type_=Text), bindparam("l", limit))
    )
    return [
        JobRun(
            id=r.id,
            job_name=r.job_name,
            started_at=r.started_at,
            finished_at=r.finished_at,
            status=r.status,
            summary=r.summary,
            error=r.error,
        )
        for r in rows
    ]


def tracked(name: str, job: JobFn) -> JobFn:
    """``job`` wrapped to record every run in ``job_runs`` (see the module docstring)."""
    if not JOB_NAME_RE.fullmatch(name):
        raise JobRegistryError(f"invalid job name {name!r}")

    @functools.wraps(job)
    async def run(ctx: dict[str, Any]) -> Any:
        sessionmaker: async_sessionmaker[AsyncSession] | None = ctx.get("sessionmaker")
        run_id: uuid.UUID | None = None
        if sessionmaker is not None:
            try:
                run_id = await start_run(sessionmaker, name)
            except Exception as exc:
                log.error("job_run_record_failed", job=name, error=type(exc).__name__)
        try:
            result = await job(ctx)
        except BaseException as exc:
            if run_id is not None and sessionmaker is not None:
                try:
                    await finish_run(
                        sessionmaker, run_id, status=STATUS_FAILED, error=type(exc).__name__
                    )
                except Exception as record_exc:
                    log.error("job_run_record_failed", job=name, error=type(record_exc).__name__)
            raise
        if run_id is not None and sessionmaker is not None:
            try:
                await finish_run(
                    sessionmaker, run_id, status=STATUS_SUCCEEDED, summary=clean_summary(result)
                )
            except Exception as exc:
                log.error("job_run_record_failed", job=name, error=type(exc).__name__)
        return result

    return run

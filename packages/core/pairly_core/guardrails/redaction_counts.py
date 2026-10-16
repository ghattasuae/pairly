# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""How many personal data values were redacted from logs and traces, per org per day.

``PiiRedactor.redact_counted`` reports how many values it masked. Two callers add that
number to the process tally ``TALLY``:

* ``redact_log_event`` (the structlog processor of the API and the worker), for the
  event's ``org_id``;
* ``LangfuseTracer.record_generation``, for the model call's organisation.

Events without an organisation (platform logs, start-up) are not counted: the count is
shown per organisation. The tally lives in memory and is thread safe. ``flush`` drains
it into ``pii_redaction_counts`` (migration 0028), one upsert per organisation and day,
in a session bound to that organisation (Row Level Security applies). Every API and
worker process flushes its own tally every minute (``start_flusher``) and at shutdown.
A failed flush puts the counts back, up to ``MAX_KEYS`` pending rows, so a database
outage loses at most what does not fit.

Only numbers are kept: no text, no field name, no kind of data.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import BigInteger, Date, DateTime, UniqueConstraint, Uuid, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, set_tenant

log = structlog.get_logger("pairly_core.guardrails.redaction_counts")

MAX_KEYS: Final = 10_000
FLUSH_SECONDS: Final = 60


class PiiRedactionCount(Base, OrgScoped):
    """One row per organisation and UTC day (migration 0028)."""

    __tablename__ = "pii_redaction_counts"
    __table_args__ = (UniqueConstraint("org_id", "day", name="uq_pii_redaction_counts_org_day"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    redactions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _org(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


class RedactionTally:
    """Counts per (organisation, UTC day), in memory. Thread safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[uuid.UUID, date], int] = {}

    def add(self, org_id: Any, n: int, *, day: date | None = None) -> None:
        org = _org(org_id)
        if org is None or n <= 0:
            return
        key = (org, day or datetime.now(UTC).date())
        with self._lock:
            if key not in self._counts and len(self._counts) >= MAX_KEYS:
                return
            self._counts[key] = self._counts.get(key, 0) + n

    def drain(self) -> dict[tuple[uuid.UUID, date], int]:
        with self._lock:
            counts, self._counts = self._counts, {}
        return counts

    def restore(self, counts: dict[tuple[uuid.UUID, date], int]) -> None:
        for (org, day), n in counts.items():
            self.add(org, n, day=day)

    def pending(self) -> int:
        with self._lock:
            return sum(self._counts.values())


TALLY: Final = RedactionTally()

_UPSERT = text(
    "INSERT INTO pii_redaction_counts (org_id, day, redactions) VALUES (:o, :d, :n) "
    "ON CONFLICT (org_id, day) DO UPDATE SET "
    "redactions = pii_redaction_counts.redactions + EXCLUDED.redactions, updated_at = now()"
)


@dataclass(frozen=True, slots=True)
class FlushResult:
    rows: int
    redactions: int
    failed_orgs: int


async def flush(
    sessionmaker: async_sessionmaker[AsyncSession], tally: RedactionTally = TALLY
) -> FlushResult:
    """Write the tally to ``pii_redaction_counts``. Never raises for one organisation."""
    counts = tally.drain()
    by_org: dict[uuid.UUID, list[tuple[date, int]]] = {}
    for (org, day), n in counts.items():
        by_org.setdefault(org, []).append((day, n))
    rows = written = failed = 0
    for org, days in by_org.items():
        try:
            async with sessionmaker() as session:
                set_tenant(session, org, None)
                async with session.begin():
                    for day, n in days:
                        await session.execute(_UPSERT, {"o": org, "d": day, "n": n})
        except Exception as exc:
            failed += 1
            tally.restore({(org, day): n for day, n in days})
            log.warning("pii_redaction_flush_failed", error=type(exc).__name__)
            continue
        rows += len(days)
        written += sum(n for _, n in days)
    return FlushResult(rows=rows, redactions=written, failed_orgs=failed)


async def _flush_every(
    sessionmaker: async_sessionmaker[AsyncSession], seconds: float, tally: RedactionTally
) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await flush(sessionmaker, tally)
        except Exception as exc:  # never let the loop die
            log.warning("pii_redaction_flush_loop_failed", error=type(exc).__name__)


def start_flusher(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    seconds: float = FLUSH_SECONDS,
    tally: RedactionTally = TALLY,
) -> asyncio.Task[None]:
    """A background task that flushes this process's tally every ``seconds``.

    Each process (every API and worker process) has its own tally, so each runs its own
    flusher; a scheduled job would run in only one of them.
    """
    return asyncio.create_task(_flush_every(sessionmaker, seconds, tally))


async def stop_flusher(
    task: asyncio.Task[None] | None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    tally: RedactionTally = TALLY,
) -> None:
    """Stop the flusher and write what is left (at shutdown)."""
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if sessionmaker is not None:
        try:
            await flush(sessionmaker, tally)
        except Exception as exc:
            log.warning("pii_redaction_flush_failed", error=type(exc).__name__)


@dataclass(frozen=True, slots=True)
class RedactionSummary:
    total: int
    days: int
    last_day: date | None


async def summary(session: AsyncSession, *, now: datetime, days: int) -> RedactionSummary:
    """The viewer's organisation's redactions over the last ``days`` days (RLS scoped)."""
    since = (now - timedelta(days=days)).date()
    total, counted, last = (
        await session.execute(
            select(
                func.coalesce(func.sum(PiiRedactionCount.redactions), 0),
                func.count(),
                func.max(PiiRedactionCount.day),
            ).where(PiiRedactionCount.day >= since)
        )
    ).one()
    return RedactionSummary(total=int(total), days=int(counted), last_day=last)

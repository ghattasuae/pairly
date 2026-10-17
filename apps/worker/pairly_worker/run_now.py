# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Run one scheduled job now, through the worker's own queue. Development and test only.

    python -m pairly_worker.run_now audit_anchor_nightly

Enqueues the named cron job (it must be one of ``RUNNABLE``) on the arq queue the worker
reads, waits until it has run and prints JSON (with its result when the worker kept one).
The worker runs it with its own context (keys, stores, database role), exactly as the
schedule would. Refuses to run unless ``PAIRLY_ENV`` is ``development`` or ``test``:
elsewhere the schedule is the only way. ``make audit-anchor-now`` runs it in the compose
worker, so the audit viewer has an anchor to check without waiting for the night.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from typing import Final

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import JobStatus

from pairly_core.ingestion.queue import job_dumps, job_loads
from pairly_worker.decision_jobs import ANCHOR_JOB

RUNNABLE: Final = frozenset({ANCHOR_JOB})
TIMEOUT_SECONDS: Final = 120
POLL_SECONDS: Final = 0.5
_WAITING: Final = frozenset({JobStatus.deferred, JobStatus.queued, JobStatus.in_progress})


async def run_now(redis_url: str, job: str) -> object:
    # The worker's JSON job format (never pickle).
    pool = await create_pool(
        RedisSettings.from_dsn(redis_url), job_serializer=job_dumps, job_deserializer=job_loads
    )
    try:
        queued = await pool.enqueue_job(job, _job_id=f"run-now:{job}:{uuid.uuid4().hex}")
        if queued is None:  # pragma: no cover - a fresh job id is never taken
            raise RuntimeError("the job was not queued")
        # The worker keeps no results: wait until the job has left the queue, then report
        # its result if one was kept (the worker log has it either way).
        deadline = asyncio.get_running_loop().time() + TIMEOUT_SECONDS
        while await queued.status() in _WAITING:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"{job} did not finish in {TIMEOUT_SECONDS} seconds")
            await asyncio.sleep(POLL_SECONDS)
        info = await queued.result_info()
        if info is None:
            return {"job": job, "finished": True}
        if not info.success:
            raise RuntimeError(f"{job} failed: {info.result!r}")
        return {"job": job, "finished": True, "result": info.result}
    finally:
        await pool.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pairly_worker.run_now", description=__doc__)
    parser.add_argument("job", choices=sorted(RUNNABLE))
    args = parser.parse_args(argv)
    if os.environ.get("PAIRLY_ENV", "production") not in {"development", "test"}:
        print("run_now only runs in development or test", file=sys.stderr)
        return 2
    redis_url = os.environ.get("PAIRLY_REDIS_URL")
    if not redis_url:
        print("PAIRLY_REDIS_URL is not set", file=sys.stderr)
        return 2
    result = asyncio.run(run_now(redis_url, args.job))
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

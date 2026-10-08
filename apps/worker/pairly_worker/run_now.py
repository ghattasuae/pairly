# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Run one scheduled job now, through the worker's own queue. Development and test only.

    python -m pairly_worker.run_now audit_anchor_nightly

Enqueues the named cron job (it must be one of ``RUNNABLE``) on the arq queue the worker
reads, waits for it and prints its result as JSON. The worker runs it with its own
context (keys, stores, database role), exactly as the schedule would. Refuses to run when
``PAIRLY_ENV`` is ``production``: there the schedule is the only way. ``make
audit-anchor-now`` runs it in the compose worker, so the audit viewer has an anchor to
check without waiting for the night.
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

from pairly_worker.decision_jobs import ANCHOR_JOB

RUNNABLE: Final = frozenset({ANCHOR_JOB})
TIMEOUT_SECONDS: Final = 120


async def run_now(redis_url: str, job: str) -> object:
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        queued = await pool.enqueue_job(job, _job_id=f"run-now:{job}:{uuid.uuid4().hex}")
        if queued is None:  # pragma: no cover - a fresh job id is never taken
            raise RuntimeError("the job was not queued")
        return await queued.result(timeout=TIMEOUT_SECONDS)
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

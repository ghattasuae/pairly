# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""arq worker entry point: ``arq pairly_worker.main.WorkerSettings``.

``WorkerSettings`` is built on first access from the environment (see
``pairly_worker.config``), so importing this module never reads settings, while
``arq`` and ``arq --check`` (the compose health check) fail closed when
``PAIRLY_REDIS_URL`` or ``PAIRLY_DATABASE_URL`` is missing.

Hardening:

* Jobs are serialised as JSON, not pickle, so data in the queue can never execute code.
* At startup the worker refuses a database role that bypasses Row Level Security.
* Cron times are UTC.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import structlog
from arq import cron, func
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.db import check_app_role, create_sessionmaker
from pairly_core.ingestion.queue import PARSE_JOB, SCAN_JOB, job_dumps, job_loads
from pairly_worker.config import WorkerConfig, load_worker_config
from pairly_worker.documents import (
    parse_document_job,
    scan_document_job,
    setup_documents,
    sweep_documents,
)
from pairly_worker.jobs import verify_audit_chains
from pairly_worker.logs import configure_logging

log = structlog.get_logger("pairly_worker")

NIGHTLY_AUDIT_JOB = "nightly_audit_verify"
SWEEP_JOB = "sweep_documents"

# Shared with the API's enqueue side (JSON, never pickle).
_dumps = job_dumps
_loads = job_loads


async def startup(ctx: dict[str, Any]) -> None:
    config: WorkerConfig = ctx["config"]
    configure_logging(config.log_level)
    engine = create_async_engine(
        config.database_url.get_secret_value(),
        pool_size=config.db_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
    )
    try:
        await check_app_role(engine)
    except Exception:
        await engine.dispose()
        raise
    ctx["engine"] = engine
    ctx["sessionmaker"] = create_sessionmaker(engine)
    try:
        setup_documents(ctx, config.env)
    except Exception:
        await engine.dispose()
        raise
    log.info("worker_startup", env=config.env.value)


async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.pop("engine", None)
    if engine is not None:
        await engine.dispose()


def build_worker_settings(config: WorkerConfig | None = None) -> type:
    """The arq settings class for ``config`` (default: from the environment)."""
    config = config or load_worker_config()
    attrs: dict[str, Any] = {
        "redis_settings": RedisSettings.from_dsn(config.redis_url.get_secret_value()),
        "functions": [
            func(verify_audit_chains, name="verify_audit_chains", max_tries=1),
            # Scan and parse fail closed inside the job; arq retries only crashes.
            func(scan_document_job, name=SCAN_JOB, max_tries=2, timeout=600),
            func(parse_document_job, name=PARSE_JOB, max_tries=2, timeout=600),
        ],
        "cron_jobs": [
            cron(
                sweep_documents,
                name=SWEEP_JOB,
                run_at_startup=True,
                unique=True,
                timeout=120,
            ),
            cron(
                verify_audit_chains,
                name=NIGHTLY_AUDIT_JOB,
                hour=config.audit_verify_hour,
                minute=config.audit_verify_minute,
                run_at_startup=False,
                unique=True,
                timeout=3600,
            ),
        ],
        "on_startup": startup,
        "on_shutdown": shutdown,
        "ctx": {"config": config},
        "health_check_interval": config.worker_health_check_interval,
        "timezone": UTC,
        "max_jobs": 4,
        "job_timeout": 600,
        "job_serializer": _dumps,
        "job_deserializer": _loads,
    }
    return type("WorkerSettings", (), attrs)


_settings: type | None = None


def __getattr__(name: str) -> type:
    global _settings
    if name == "WorkerSettings":
        if _settings is None:
            _settings = build_worker_settings()
        return _settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

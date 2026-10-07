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
* At startup the worker loads ``PAIRLY_AUDIT_SIGNING_KEY`` (fails closed when missing)
  and, outside tests, the audit anchor store (``PAIRLY_AUDIT_ANCHOR_*``).
* Cron times are UTC.
* Every cron job is declared in the job registry (``pairly_core.jobs.registry``) and
  wrapped with ``tracked``, so each run is recorded in ``job_runs``. A job that is not
  declared is refused when the settings are built.
"""

from __future__ import annotations

import os
from datetime import UTC
from typing import Any, cast

import structlog
from arq import cron, func
from arq.connections import RedisSettings
from arq.typing import WorkerCoroutine
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.approvals.hooks import ApprovalHook, ApprovalHooks
from pairly_core.audit_anchor.signing import PUBLIC_KEY_ENV, SIGNING_KEY_ENV, load_signing_key
from pairly_core.audit_anchor.store import S3AnchorStore, load_anchor_settings
from pairly_core.config import ConfigError, Environment
from pairly_core.crypto.keys import init_crypto
from pairly_core.db import check_app_role, create_sessionmaker
from pairly_core.decisions.hooks import DecisionRecordHook
from pairly_core.flags.registry import build_flag_registry, install_flag_registry
from pairly_core.ingestion.queue import PARSE_JOB, SCAN_JOB, job_dumps, job_loads
from pairly_core.integrations.slack.transport import HttpSlackTransport
from pairly_core.jobs.registry import (
    APPROVAL_SLA_ESCALATION,
    DELIVER_WEBHOOKS,
    NIGHTLY_AUDIT_VERIFY,
    NOTIFICATION_DELIVERY,
    NOTIFICATION_DIGEST,
    RETENTION_DRY_RUN,
    SWEEP_DOCUMENTS,
    WORKFLOW_RESUME_RECONCILE,
    JobRegistry,
    build_job_registry,
)
from pairly_core.jobs.runs import JobFn, tracked
from pairly_core.notifications.approval_hook import NotificationApprovalHook
from pairly_core.notifications.delivery import Transports
from pairly_core.notifications.email import email_transport_from_settings
from pairly_core.notifications.settings import load_notification_settings
from pairly_core.plugins import discover_plugins
from pairly_core.rbac import build_registry
from pairly_core.retention.rules import build_retention_registry
from pairly_core.search.documents import PURGE_JOB
from pairly_core.webhooks.events import build_event_registry, install_event_registry
from pairly_core.workflow.checkpoint import TenantCheckpointer
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_worker import decision_jobs
from pairly_worker.config import WorkerConfig, load_worker_config
from pairly_worker.documents import (
    parse_document_job,
    purge_document_search_job,
    scan_document_job,
    setup_documents,
    sweep_documents,
)
from pairly_worker.jobs import verify_audit_chains
from pairly_worker.logs import configure_logging
from pairly_worker.notification_jobs import deliver_notifications, send_digests
from pairly_worker.retention_jobs import REGISTRY as RETENTION_REGISTRY
from pairly_worker.retention_jobs import retention_dry_run
from pairly_worker.webhook_jobs import deliver_webhooks, setup_webhooks
from pairly_worker.workflow_jobs import escalate_overdue_approvals, resume_decided_runs

log = structlog.get_logger("pairly_worker")

NIGHTLY_AUDIT_JOB = NIGHTLY_AUDIT_VERIFY
SWEEP_JOB = SWEEP_DOCUMENTS
APPROVAL_SLA_JOB = APPROVAL_SLA_ESCALATION
WORKFLOW_RESUME_JOB = WORKFLOW_RESUME_RECONCILE
WEBHOOK_JOB = DELIVER_WEBHOOKS
RETENTION_JOB = RETENTION_DRY_RUN
NOTIFICATION_DELIVERY_JOB = NOTIFICATION_DELIVERY
NOTIFICATION_DIGEST_JOB = NOTIFICATION_DIGEST

# Shared with the API's enqueue side (JSON, never pickle).
_dumps = job_dumps
_loads = job_loads


def setup_audit_anchors(ctx: dict[str, Any], config: WorkerConfig) -> None:
    """The signing key (always) and the anchor store (outside tests: required)."""
    ctx["audit_signing_key"] = load_signing_key(
        {
            SIGNING_KEY_ENV: config.audit_signing_key.get_secret_value(),
            PUBLIC_KEY_ENV: os.environ.get(PUBLIC_KEY_ENV, ""),
        }
    )
    try:
        ctx["anchor_store"] = S3AnchorStore.from_settings(load_anchor_settings())
    except ConfigError:
        if config.env is not Environment.TEST:
            raise
        log.warning("audit_anchors_disabled", reason="anchor storage settings missing")
        ctx["anchor_store"] = None


async def startup(ctx: dict[str, Any]) -> None:
    config: WorkerConfig = ctx["config"]
    configure_logging(config.log_level)
    # Fail closed: no encryption keys, no worker (the keys are needed to read and write
    # encrypted fields). Same for the audit signing key.
    init_crypto()
    setup_audit_anchors(ctx, config)
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
    sessionmaker = create_sessionmaker(engine)
    ctx["sessionmaker"] = sessionmaker
    try:
        # Both fail closed: missing ingestion settings (outside tests) or an invalid
        # workflow plugin stop the worker before it takes any job.
        setup_documents(ctx, config.env)
        # Fail closed: invalid notification settings stop the worker too.
        notify = load_notification_settings(env=config.env)
        ctx["transports"] = Transports(
            settings=notify,
            email=email_transport_from_settings(notify),
            slack=HttpSlackTransport(base_url=notify.slack_api_base_url),
        )
        # Decision records first (a hook that raises aborts the change), then the
        # approval notifications everywhere but in tests.
        hook_list: list[ApprovalHook] = [DecisionRecordHook()]
        if config.env is not Environment.TEST:
            hook_list.append(
                NotificationApprovalHook(notify, link_ttl_seconds=config.approval_link_ttl_seconds)
            )
        hooks = ApprovalHooks(hook_list)
        plugins = list(discover_plugins().values())
        registry = build_registry(plugins)
        install_event_registry(build_event_registry(plugins))
        install_flag_registry(build_flag_registry(plugins))
        ctx[RETENTION_REGISTRY] = build_retention_registry(plugins)
        setup_webhooks(ctx)
        runtime = WorkflowRuntime.from_plugins(
            plugins,
            sessionmaker=sessionmaker,
            checkpointer=TenantCheckpointer(config.database_url.get_secret_value()),
            registry=registry,
            hooks=hooks,
        )
        await runtime.check()
    except Exception:
        await engine.dispose()
        raise
    ctx["registry"] = registry
    ctx["runtime"] = runtime
    log.info("worker_startup", env=config.env.value)


async def shutdown(ctx: dict[str, Any]) -> None:
    transports = ctx.pop("transports", None)
    for transport in (getattr(transports, "email", None), getattr(transports, "slack", None)):
        close = getattr(transport, "aclose", None)
        if close is not None:
            await close()
    sender = ctx.pop("webhook_sender", None)
    if sender is not None:
        await sender.aclose()
    engine = ctx.pop("engine", None)
    if engine is not None:
        await engine.dispose()


def _job(registry: JobRegistry, name: str, job: JobFn) -> WorkerCoroutine:
    """``job`` recorded in ``job_runs``, once its name is declared in ``registry``."""
    registry.require(name)
    return cast("WorkerCoroutine", tracked(name, job))


def build_worker_settings(config: WorkerConfig | None = None) -> type:
    """The arq settings class for ``config`` (default: from the environment)."""
    config = config or load_worker_config()
    jobs = build_job_registry()
    attrs: dict[str, Any] = {
        "redis_settings": RedisSettings.from_dsn(config.redis_url.get_secret_value()),
        "functions": [
            func(verify_audit_chains, name="verify_audit_chains", max_tries=1),
            # Scan and parse fail closed inside the job; arq retries only crashes.
            func(scan_document_job, name=SCAN_JOB, max_tries=2, timeout=600),
            func(parse_document_job, name=PARSE_JOB, max_tries=2, timeout=600),
            # Queued by the flags route when search.documents is turned off.
            func(purge_document_search_job, name=PURGE_JOB, max_tries=3, timeout=600),
            *decision_jobs.functions(),
        ],
        "cron_jobs": [
            cron(
                _job(jobs, SWEEP_JOB, sweep_documents),
                name=SWEEP_JOB,
                run_at_startup=True,
                unique=True,
                timeout=120,
            ),
            cron(
                _job(jobs, NIGHTLY_AUDIT_JOB, verify_audit_chains),
                name=NIGHTLY_AUDIT_JOB,
                hour=config.audit_verify_hour,
                minute=config.audit_verify_minute,
                run_at_startup=False,
                unique=True,
                timeout=3600,
            ),
            cron(
                _job(jobs, APPROVAL_SLA_JOB, escalate_overdue_approvals),
                name=APPROVAL_SLA_JOB,
                minute=set(range(0, 60, 5)),
                run_at_startup=False,
                unique=True,
                timeout=240,
            ),
            cron(
                _job(jobs, NOTIFICATION_DELIVERY_JOB, deliver_notifications),
                name=NOTIFICATION_DELIVERY_JOB,
                run_at_startup=True,
                unique=True,
                timeout=300,
            ),
            cron(
                _job(jobs, NOTIFICATION_DIGEST_JOB, send_digests),
                name=NOTIFICATION_DIGEST_JOB,
                hour=config.digest_hour_utc,
                minute=0,
                run_at_startup=False,
                unique=True,
                timeout=1800,
            ),
            cron(
                _job(jobs, WORKFLOW_RESUME_JOB, resume_decided_runs),
                name=WORKFLOW_RESUME_JOB,
                run_at_startup=True,
                unique=True,
                timeout=600,
            ),
            # Outbound webhooks: fan out the outbox and send due deliveries.
            cron(
                _job(jobs, WEBHOOK_JOB, deliver_webhooks),
                name=WEBHOOK_JOB,
                run_at_startup=True,
                unique=True,
                timeout=300,
            ),
            # Retention: counts only (dry run), never deletes.
            cron(
                _job(jobs, RETENTION_JOB, retention_dry_run),
                name=RETENTION_JOB,
                hour=2,
                minute=15,
                run_at_startup=False,
                unique=True,
                timeout=1800,
            ),
            # Decision records, appeals and audit hardening (WP1.10, WP1.10b).
            *decision_jobs.cron_jobs(config, lambda name, job: _job(jobs, name, job)),
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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Worker settings: fail closed on missing config, safe arq options, lazy settings class."""

from __future__ import annotations

import base64
import secrets
from datetime import UTC
from typing import Any

import pytest
from arq.cron import CronJob
from arq.worker import Function

import pairly_worker.main as worker_main
from pairly_core.config import ConfigError
from pairly_core.jobs.registry import JobRegistryError, build_job_registry
from pairly_core.plugins import discover_plugins
from pairly_worker.config import load_worker_config
from pairly_worker.decision_jobs import (
    ANCHOR_JOB,
    DECISION_SWEEP_JOB,
    DENIAL_FLUSH_JOB,
)
from pairly_worker.decision_jobs import APPEAL_SLA_JOB as APPEAL_JOB
from pairly_worker.main import (
    APPROVAL_SLA_JOB,
    NIGHTLY_AUDIT_JOB,
    NOTIFICATION_DELIVERY_JOB,
    NOTIFICATION_DIGEST_JOB,
    RETENTION_JOB,
    SWEEP_JOB,
    WEBHOOK_JOB,
    WORKFLOW_RESUME_JOB,
    build_worker_settings,
)

DB = "postgresql://pairly_app:pw@db:5432/pairly"
REDIS = "redis://:secret-pass@valkey:6379/0"
# A fresh Ed25519 seed per run (never a fixed key).
SIGNING_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PAIRLY_DATABASE_URL",
        "PAIRLY_REDIS_URL",
        "PAIRLY_AUDIT_VERIFY_HOUR",
        "PAIRLY_AUDIT_SIGNING_KEY",
        "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_settings_fail_closed() -> None:
    with pytest.raises(ConfigError) as exc:
        load_worker_config()
    assert "PAIRLY_DATABASE_URL" in str(exc.value)
    assert "PAIRLY_REDIS_URL" in str(exc.value)
    assert "PAIRLY_AUDIT_SIGNING_KEY" in str(exc.value)


def test_invalid_signing_key_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as exc:
        load_worker_config(database_url=DB, redis_url=REDIS, audit_signing_key="not-a-key!")
    assert "PAIRLY_AUDIT_SIGNING_KEY" in str(exc.value)
    assert "not-a-key" not in str(exc.value)


def test_invalid_redis_url_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ConfigError) as exc:
        load_worker_config(
            database_url=DB, redis_url="http://:hunter2@host/0", audit_signing_key=SIGNING_KEY
        )
    assert "PAIRLY_REDIS_URL" in str(exc.value)
    assert "hunter2" not in str(exc.value)


def test_approval_link_ttl_matches_the_api_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAIRLY_APPROVAL_LINK_TTL_SECONDS", raising=False)
    assert (
        load_worker_config(
            database_url=DB, redis_url=REDIS, audit_signing_key=SIGNING_KEY
        ).approval_link_ttl_seconds
        == 1800
    )
    monkeypatch.setenv("PAIRLY_APPROVAL_LINK_TTL_SECONDS", "600")
    assert (
        load_worker_config(
            database_url=DB, redis_url=REDIS, audit_signing_key=SIGNING_KEY
        ).approval_link_ttl_seconds
        == 600
    )
    with pytest.raises(ConfigError):
        load_worker_config(
            database_url=DB,
            redis_url=REDIS,
            audit_signing_key=SIGNING_KEY,
            approval_link_ttl_seconds=10,
        )


def test_settings_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIRLY_DATABASE_URL", DB)
    monkeypatch.setenv("PAIRLY_REDIS_URL", REDIS)
    monkeypatch.setenv("PAIRLY_AUDIT_VERIFY_HOUR", "3")
    monkeypatch.setenv("PAIRLY_AUDIT_SIGNING_KEY", SIGNING_KEY)
    settings: Any = build_worker_settings()
    assert settings.redis_settings.host == "valkey"
    assert settings.redis_settings.password == "secret-pass"  # noqa: S105 (test value)
    assert settings.timezone is UTC
    assert settings.health_check_interval == 30
    jobs = {job.name: job for job in settings.cron_jobs}
    assert set(jobs) == {
        NIGHTLY_AUDIT_JOB,
        SWEEP_JOB,
        APPROVAL_SLA_JOB,
        WORKFLOW_RESUME_JOB,
        NOTIFICATION_DELIVERY_JOB,
        NOTIFICATION_DIGEST_JOB,
        ANCHOR_JOB,
        APPEAL_JOB,
        DENIAL_FLUSH_JOB,
        DECISION_SWEEP_JOB,
        WEBHOOK_JOB,
        RETENTION_JOB,
        "expense_stuck_note_sweep",
        "hire_screening_start",
    }
    assert jobs[NOTIFICATION_DIGEST_JOB].hour == 7
    assert jobs[NOTIFICATION_DELIVERY_JOB].unique
    assert (jobs[ANCHOR_JOB].hour, jobs[ANCHOR_JOB].minute) == (2, 10)
    # Every scheduled job is declared in the job registry, and every declared core job
    # is scheduled (the operations view lists exactly what runs).
    assert set(jobs) == {j.name for j in build_job_registry(discover_plugins().values()).jobs}
    # Plugin jobs with a coroutine run too (expense: every 5 minutes, one at a time).
    sweep = jobs["expense_stuck_note_sweep"]
    assert (sweep.minute, sweep.unique) == (set(range(0, 60, 5)), True)
    # Hire: start pending screenings every minute, one at a time.
    screening = jobs["hire_screening_start"]
    assert (screening.minute, screening.unique) == (set(range(60)), True)
    # Each cron coroutine is wrapped to record its runs in job_runs.
    assert all(getattr(job.coroutine, "__wrapped__", None) is not None for job in jobs.values())
    # The retention dry run is daily at 02:15 UTC, one at a time.
    retention = jobs[RETENTION_JOB]
    assert (retention.hour, retention.minute, retention.unique) == (2, 15, True)
    # Webhook delivery runs every minute, one at a time.
    assert jobs[WEBHOOK_JOB].unique
    assert jobs[WEBHOOK_JOB].second == 0
    assert jobs[WEBHOOK_JOB].minute is None
    cron_job = jobs[NIGHTLY_AUDIT_JOB]
    assert isinstance(cron_job, CronJob)
    assert jobs[APPROVAL_SLA_JOB].minute == set(range(0, 60, 5))
    assert jobs[APPROVAL_SLA_JOB].unique
    assert (cron_job.name, cron_job.hour, cron_job.minute, cron_job.unique) == (
        NIGHTLY_AUDIT_JOB,
        3,
        30,
        True,
    )
    functions = {f.name: f for f in settings.functions}
    assert set(functions) == {
        "verify_audit_chains",
        "scan_document",
        "parse_document",
        "compliance_export",
        "verify_audit_chain",
        "purge_document_search",
    }
    assert all(isinstance(f, Function) for f in functions.values())
    assert jobs[SWEEP_JOB].unique
    # JSON, never pickle: queue data cannot run code.
    payload = {"a": [1, "x"]}
    assert settings.job_deserializer(settings.job_serializer(payload)) == payload
    assert (
        settings.ctx["config"].database_url.get_secret_value().startswith("postgresql+asyncpg://")
    )


def test_lazy_module_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "_settings", None)
    with pytest.raises(ConfigError):
        _ = worker_main.WorkerSettings
    monkeypatch.setenv("PAIRLY_DATABASE_URL", DB)
    monkeypatch.setenv("PAIRLY_REDIS_URL", REDIS)
    monkeypatch.setenv("PAIRLY_AUDIT_SIGNING_KEY", SIGNING_KEY)
    first = worker_main.WorkerSettings
    assert worker_main.WorkerSettings is first
    with pytest.raises(AttributeError):
        _ = worker_main.NotThere


def test_undeclared_jobs_are_refused() -> None:
    with pytest.raises(JobRegistryError, match="unknown job"):
        worker_main._job(build_job_registry(), "not_declared", worker_main.retention_dry_run)

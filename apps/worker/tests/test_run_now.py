# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``python -m pairly_worker.run_now``: development only, known jobs only."""

from __future__ import annotations

import pytest

from pairly_worker import run_now
from pairly_worker.decision_jobs import ANCHOR_JOB


def test_refuses_outside_development(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAIRLY_REDIS_URL", "redis://localhost:6379/0")
    for env in ("production", "staging"):
        monkeypatch.setenv("PAIRLY_ENV", env)
        assert run_now.main([ANCHOR_JOB]) == 2
    monkeypatch.delenv("PAIRLY_ENV")
    assert run_now.main([ANCHOR_JOB]) == 2
    assert "only runs in development or test" in capsys.readouterr().err


def test_refuses_unknown_jobs_and_a_missing_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIRLY_ENV", "development")
    with pytest.raises(SystemExit):
        run_now.main(["retention_purge"])
    monkeypatch.delenv("PAIRLY_REDIS_URL", raising=False)
    assert run_now.main([ANCHOR_JOB]) == 2
    assert frozenset({ANCHOR_JOB}) == run_now.RUNNABLE


def test_enqueues_the_job_and_returns_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class Job:
        async def result(self, timeout: float) -> dict[str, int]:
            return {"written": 1}

    class Pool:
        async def enqueue_job(self, name: str, _job_id: str) -> Job:
            calls.append((name, _job_id))
            return Job()

        async def aclose(self) -> None:
            calls.append(("closed", ""))

    async def fake_pool(settings: object) -> Pool:
        return Pool()

    monkeypatch.setattr(run_now, "create_pool", fake_pool)
    monkeypatch.setenv("PAIRLY_ENV", "development")
    monkeypatch.setenv("PAIRLY_REDIS_URL", "redis://localhost:6379/0")
    assert run_now.main([ANCHOR_JOB]) == 0
    assert calls[0][0] == ANCHOR_JOB
    assert calls[0][1].startswith(f"run-now:{ANCHOR_JOB}:")
    assert calls[-1] == ("closed", "")

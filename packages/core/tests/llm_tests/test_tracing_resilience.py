# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Redaction before tracing, the LangFuse adapter (mocked client), retries and breaker."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.resilience import BreakerState, CircuitBreaker, RetryPolicy, call_with_retry
from pairly_core.llm.settings import LLMSettings
from pairly_core.llm.tracing import (
    GenerationRecord,
    LangfuseTracer,
    NoopTracer,
    RegexRedactor,
    build_tracer,
)
from pairly_core.llm.types import TaskSpec

from .conftest import HAIKU, ORG, Answer, make_harness

PII = {
    "text": "Jane <jane.doe@example.com>, +971 50 123 4567, IBAN AE07 0331 2345 6789 0123 456",
    "nested": [{"email": "a.b@c.io"}, "call 0044 20 7946 0958"],
    "amount": 1200,
}


def test_regex_redactor_masks_email_phone_iban() -> None:
    out = RegexRedactor().redact(PII)
    text = out["text"]
    assert "jane.doe@example.com" not in text
    assert "[EMAIL]" in text
    assert "123 4567" not in text
    assert "[PHONE]" in text
    assert "AE07" not in text
    assert "[IBAN]" in text
    assert out["nested"][0]["email"] == "[EMAIL]"
    assert "7946" not in out["nested"][1]
    assert out["amount"] == 1200
    # Ordinary numbers and words survive.
    assert RegexRedactor().redact("Invoice 2026-10 total 1200.50 EUR") == (
        "Invoice 2026-10 total 1200.50 EUR"
    )
    assert RegexRedactor().redact(("x",)) == ["x"]


class FakeGeneration:
    def __init__(self, sink: list[tuple[str, dict[str, Any]]]) -> None:
        self.sink = sink

    def update(self, **kwargs: Any) -> None:
        self.sink.append(("update", kwargs))

    def end(self) -> None:
        self.sink.append(("end", {}))


class FakeLangfuse:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.flushed = 0

    def start_observation(self, **kwargs: Any) -> FakeGeneration:
        self.calls.append(("start", kwargs))
        return FakeGeneration(self.calls)

    def flush(self) -> None:
        self.flushed += 1


async def test_langfuse_receives_only_redacted_data(prompt_root: Path) -> None:
    client = FakeLangfuse()
    harness = make_harness(PromptRegistry([prompt_root]))
    harness.router._tracer = LangfuseTracer(client, RegexRedactor())
    harness.anthropic.script(HAIKU, {"label": "jane.doe@example.com", "confidence": 0.9})
    await harness.router.complete(
        task=TaskSpec("classify", "v1"), inputs=PII, schema=Answer, org_id=ORG
    )
    exported = repr(client.calls)
    for secret in ("jane.doe@example.com", "a.b@c.io", "123 4567", "7946", "AE07"):
        assert secret not in exported
    start = client.calls[0][1]
    assert start["as_type"] == "generation"
    assert start["model"] == HAIKU
    assert start["version"] == "v1"
    assert start["metadata"]["task"] == "classify"
    update = client.calls[1][1]
    assert update["output"]["label"] == "[EMAIL]"
    assert update["usage_details"] == {"input": 100, "output": 20}
    assert update["cost_details"] == {"total": 0.0002}
    assert client.calls[2][0] == "end"
    harness.router._tracer.flush()
    assert client.flushed == 1


def test_tracing_failures_never_raise() -> None:
    class Broken:
        def start_observation(self, **_: Any) -> Any:
            raise RuntimeError("langfuse down")

        def flush(self) -> None:
            raise RuntimeError("langfuse down")

    tracer = LangfuseTracer(Broken(), RegexRedactor())
    record = GenerationRecord(
        name="llm.t",
        chain_id="c",
        call_id=None,
        org_id="o",
        task="t",
        prompt_version="v1",
        prompt_hash="h",
        provider="p",
        model="m",
        tier="small",
        status="ok",
        input={},
        output={},
        input_tokens=0,
        output_tokens=0,
        cost_micros=0,
        latency_ms=0,
        from_recording=False,
    )
    tracer.record_generation(record)
    tracer.flush()
    NoopTracer().record_generation(record)
    NoopTracer().flush()


def test_build_tracer_is_noop_without_langfuse_env() -> None:
    assert isinstance(build_tracer(LLMSettings(), RegexRedactor()), NoopTracer)


def test_build_tracer_uses_langfuse_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import langfuse

    created: list[dict[str, Any]] = []

    class StubLangfuse(FakeLangfuse):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__()
            created.append(kwargs)

    # Never start the real SDK (background threads, network) in tests.
    monkeypatch.setattr(langfuse, "Langfuse", StubLangfuse)
    settings = LLMSettings(
        langfuse_host="http://langfuse.test",
        langfuse_public_key=SecretStr("pk-x"),
        langfuse_secret_key=SecretStr("sk-x"),
    )
    tracer = build_tracer(settings, RegexRedactor())
    assert isinstance(tracer, LangfuseTracer)
    (kwargs,) = created
    assert (kwargs["public_key"], kwargs["secret_key"], kwargs["host"]) == (
        "pk-x",
        "sk-x",
        "http://langfuse.test",
    )
    # The SDK-level mask redacts too (second layer).
    assert kwargs["mask"](data={"e": "x@y.io"}) == {"e": "[EMAIL]"}

    def boom(**_: Any) -> Any:
        raise RuntimeError("bad config")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    assert isinstance(build_tracer(settings, RegexRedactor()), NoopTracer)


async def test_retry_policy_backoff_and_classification() -> None:
    policy = RetryPolicy(max_retries=3, base_delay=1.0, max_delay=3.0)
    rng = random.Random(1)  # noqa: S311  (jitter, not crypto)
    for n, cap in ((1, 1.0), (2, 2.0), (3, 3.0), (6, 3.0)):
        assert 0 <= policy.delay(n, rng) <= cap

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderError(ErrorKind.TIMEOUT)
        return "ok"

    assert await call_with_retry(flaky, policy, sleep=sleep) == ("ok", 2)
    assert len(sleeps) == 2

    async def bad_request() -> str:
        raise ProviderError(ErrorKind.BAD_REQUEST, status=400)

    with pytest.raises(ProviderError):
        await call_with_retry(bad_request, policy, sleep=sleep)
    assert len(sleeps) == 2  # no retry for a non-retryable error

    async def always_429() -> str:
        raise ProviderError(ErrorKind.RATE_LIMITED, status=429)

    with pytest.raises(ProviderError):
        await call_with_retry(always_429, policy, sleep=sleep, may_retry=lambda: False)
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    ("kind", "retryable", "breaker"),
    [
        (ErrorKind.RATE_LIMITED, True, True),
        (ErrorKind.SERVER_ERROR, True, True),
        (ErrorKind.TIMEOUT, True, True),
        (ErrorKind.CONNECTION, True, True),
        (ErrorKind.AUTH, False, True),
        (ErrorKind.BAD_REQUEST, False, False),
        (ErrorKind.NOT_FOUND, False, False),
        (ErrorKind.UNKNOWN, False, False),
    ],
)
def test_error_kinds(kind: ErrorKind, retryable: bool, breaker: bool) -> None:
    error = ProviderError(kind)
    assert error.retryable is retryable
    assert error.counts_for_breaker is breaker


def test_circuit_breaker_states() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10, clock=lambda: now[0])
    failure = ProviderError(ErrorKind.SERVER_ERROR)
    assert breaker.allow()
    breaker.record_failure(ProviderError(ErrorKind.BAD_REQUEST))  # ignored
    breaker.record_failure(failure)
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure(failure)
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()
    now[0] = 10
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()
    assert not breaker.allow()  # one trial at a time
    breaker.record_failure(failure)  # trial failed: open again at once
    assert breaker.state is BreakerState.OPEN
    now[0] = 20
    assert breaker.allow()
    breaker.release()  # trial ended without a health signal
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Routing, escalation, fallback, breaker, modes and replay with scripted providers."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pairly_core.db import TenantSession
from pairly_core.errors import NotFoundError
from pairly_core.llm.errors import (
    BudgetExceeded,
    ErrorKind,
    InputTooLarge,
    ModelUnavailable,
    ProviderError,
    RecordingMissing,
)
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider, FakeReply
from pairly_core.llm.resilience import BreakerState
from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.store import RAW_TEXT_KEY
from pairly_core.llm.types import CallContext, TaskSpec

from .conftest import HAIKU, LUNA, ORG, SOL, SONNET, Answer, Harness, MemoryStore, Plain, StubBudget

TASK = TaskSpec(name="classify", prompt_version="v1", confidence_threshold=0.7)
INPUTS = {"text": "Invoice from ACME"}
GOOD = {"label": "invoice", "confidence": 0.95}


async def test_cheap_model_first_and_metadata(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(
        task=TASK,
        inputs=INPUTS,
        schema=Answer,
        org_id=ORG,
        context=CallContext(workflow="pay", entity_type="invoice", entity_id="inv_1"),
    )
    assert isinstance(result.value, Answer)
    assert result.value.label == "invoice"
    assert (result.provider, result.model, result.tier) == ("anthropic", HAIKU, "small")
    assert result.prompt_version == "v1"
    assert len(result.prompt_hash) == 64
    assert result.confidence == 0.95
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    # Haiku 4.5: $1 / $5 per MTok -> 100 * 1 + 20 * 5 micro-USD.
    assert result.cost_micros == 200
    assert result.total_cost_micros == 200
    assert not result.from_recording
    assert [a.outcome for a in result.attempts] == ["ok"]
    assert harness.anthropic.calls_for(SONNET) == 0
    (row,) = harness.store.rows
    assert row["status"] == "ok"
    assert row["mode"] == "live"
    assert row["workflow"] == "pay"
    assert row["request"]["inputs"] == INPUTS
    assert row["request"]["settings"]["temperature"] == 0.0
    assert row["cost_micros"] == 200
    assert harness.budget.settled == [200]
    assert not harness.budget.reserved
    # Temperature 0 is sent only where the model accepts it.
    assert harness.anthropic.requests[0].temperature == 0.0


async def test_validation_failure_escalates_to_next_tier(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "x" * 100, "confidence": 0.9})
    harness.anthropic.script(SONNET, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert (result.model, result.tier) == (SONNET, "large")
    assert [a.outcome for a in result.attempts] == ["invalid_output", "ok"]
    assert result.attempts[0].error_kind == "schema_validation"
    # Escalation goes up a tier, not sideways to the other provider.
    assert harness.openai.requests == []
    assert harness.store.rows[0]["chain_id"] == harness.store.rows[1]["chain_id"]
    assert [r["attempt"] for r in harness.store.rows] == [1, 2]
    assert result.total_cost_micros == 200 + (100 * 2 + 20 * 10)
    # Sonnet 5.5 rejects a non-default temperature, so none is sent.
    assert harness.anthropic.requests[1].temperature is None


async def test_invalid_json_and_incomplete_output_escalate(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, "not json at all")
    harness.anthropic.script(
        SONNET,
        FakeReply(
            text='{"label": "invoice", "confidence": 0.9}', complete=False, stop_reason="max_tokens"
        ),
    )
    with pytest.raises(ModelUnavailable):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    kinds = [r["error_kind"] for r in harness.store.rows]
    assert kinds == ["invalid_json", "incomplete:max_tokens"]
    assert harness.store.rows[0]["response"] == {RAW_TEXT_KEY: "not json at all"}


async def test_low_confidence_escalates_and_fails_closed(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice", "confidence": 0.4})
    harness.anthropic.script(SONNET, {"label": "invoice", "confidence": 0.5})
    with pytest.raises(ModelUnavailable) as info:
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert not isinstance(info.value, RecordingMissing | BudgetExceeded)
    assert [r["status"] for r in harness.store.rows] == ["low_confidence", "low_confidence"]
    assert [r["confidence"] for r in harness.store.rows] == [0.4, 0.5]


async def test_low_confidence_recovers_on_large_tier(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice", "confidence": 0.4})
    harness.anthropic.script(SONNET, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.tier == "large"
    assert result.confidence == 0.95


async def test_threshold_requires_a_confidence_field(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice"})
    harness.anthropic.script(SONNET, {"label": "invoice"})
    task = TaskSpec(name="plain", prompt_version="v1", confidence_threshold=0.5)
    with pytest.raises(ModelUnavailable):
        await harness.router.complete(task=task, inputs=INPUTS, schema=Plain, org_id=ORG)
    assert {r["error_kind"] for r in harness.store.rows} == {"confidence_missing"}


async def test_no_threshold_accepts_any_valid_output(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice"})
    task = TaskSpec(name="plain", prompt_version="v1")
    result = await harness.router.complete(task=task, inputs=INPUTS, schema=Plain, org_id=ORG)
    assert result.confidence is None


async def test_max_tier_limits_escalation(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice", "confidence": 0.1})
    harness.anthropic.script(SONNET, GOOD)
    task = TaskSpec(
        name="classify", prompt_version="v1", confidence_threshold=0.7, max_tier="small"
    )
    with pytest.raises(ModelUnavailable):
        await harness.router.complete(task=task, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert harness.anthropic.calls_for(SONNET) == 0


async def test_provider_fallback_when_primary_is_down(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, ProviderError(ErrorKind.SERVER_ERROR, "HTTP 529", status=529))
    harness.openai.script(LUNA, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert (result.provider, result.model, result.tier) == ("openai", LUNA, "small")
    # 1 try + 2 retries on the primary, with growing jittered backoff.
    assert harness.anthropic.calls_for(HAIKU) == 3
    assert len(harness.sleeps) == 2
    assert 0 <= harness.sleeps[0] <= 0.5
    assert 0 <= harness.sleeps[1] <= 1.0
    assert [a.outcome for a in result.attempts] == ["provider_error", "ok"]
    assert harness.store.rows[0]["error_kind"] == "server_error"
    assert harness.store.rows[0]["cost_micros"] == 0
    # The failed attempt released its reservation at zero cost.
    assert harness.budget.settled == [0, 100 * 0 + 10 + 10]


async def test_unavailable_provider_is_skipped(harness: Harness) -> None:
    harness.anthropic.set_available(False)
    harness.openai.script(LUNA, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.provider == "openai"
    assert [a.outcome for a in result.attempts] == ["unavailable", "ok"]
    assert harness.anthropic.requests == []
    # Skipped attempts write no row.
    assert len(harness.store.rows) == 1


async def test_everything_down_fails_closed(harness: Harness) -> None:
    down = ProviderError(ErrorKind.CONNECTION, "refused")
    for model in (HAIKU, SONNET):
        harness.anthropic.script(model, down)
    for model in (LUNA, SOL):
        harness.openai.script(model, down)
    with pytest.raises(ModelUnavailable):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert not harness.budget.reserved


async def test_non_retryable_error_is_not_retried(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, ProviderError(ErrorKind.BAD_REQUEST, "HTTP 400", status=400))
    harness.openai.script(LUNA, GOOD)
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert harness.anthropic.calls_for(HAIKU) == 1
    assert harness.sleeps == []
    # Our own bad request says nothing about provider health.
    assert harness.router.breakers["anthropic"].state is BreakerState.CLOSED


async def test_unexpected_provider_exception_is_contained(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, RuntimeError("bug in provider"))
    harness.openai.script(LUNA, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.provider == "openai"
    assert harness.store.rows[0]["error_kind"] == "unknown"


async def test_circuit_breaker_opens_and_recovers(harness: Harness) -> None:
    breaker = harness.router.breakers["anthropic"]
    harness.anthropic.script(HAIKU, ProviderError(ErrorKind.RATE_LIMITED, "HTTP 429", status=429))
    harness.openai.script(LUNA, GOOD)
    # Threshold 5: two complete() calls send 3 + 2 failing requests (retries stop
    # once the breaker opens).
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert breaker.state is BreakerState.CLOSED
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert breaker.state is BreakerState.OPEN
    assert harness.anthropic.calls_for(HAIKU) == 5

    # While open, the provider is not called at all.
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.attempts[0].outcome == "circuit_open"
    assert harness.anthropic.calls_for(HAIKU) == 5

    # After the reset time one trial goes through; success closes the breaker.
    harness.now[0] += 31
    assert breaker.state is BreakerState.HALF_OPEN
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.provider == "anthropic"
    assert breaker.state is BreakerState.CLOSED


async def test_budget_exceeded_stops_the_chain(harness: Harness) -> None:
    harness.budget.limit_micros = 10  # less than any worst case
    harness.anthropic.script(HAIKU, GOOD)
    with pytest.raises(BudgetExceeded):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert harness.anthropic.requests == []
    assert harness.openai.requests == []
    assert harness.store.rows == []
    assert isinstance(BudgetExceeded(), ModelUnavailable)


async def test_reservation_is_an_upper_bound(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    (reserved,) = harness.budget.reservations
    assert reserved >= result.cost_micros
    # max_output_tokens (512) at $5 per MTok alone.
    assert reserved >= 512 * 5


async def test_inputs_must_be_json_and_bounded(harness: Harness) -> None:
    with pytest.raises(ValueError, match="plain JSON"):
        await harness.router.complete(task=TASK, inputs={"x": object()}, schema=Answer, org_id=ORG)
    with pytest.raises(ValueError, match="plain JSON"):
        await harness.router.complete(
            task=TASK, inputs={"x": float("nan")}, schema=Answer, org_id=ORG
        )
    with pytest.raises(InputTooLarge):
        await harness.router.complete(
            task=TASK, inputs={"x": "a" * 2_000_000}, schema=Answer, org_id=ORG
        )
    with pytest.raises(ValueError, match="expects Answer"):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Plain, org_id=ORG)


async def test_oversized_response_is_rejected(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice", "confidence": 0.9, "pad": "x" * 300_000})
    harness.anthropic.script(SONNET, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.tier == "large"
    assert harness.store.rows[0]["error_kind"] == "response_too_large"
    assert harness.store.rows[0]["response"] is None


# ------------------------------------------------------------------ modes


async def test_recorded_mode_hit_replays_without_calling(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "x" * 100, "confidence": 0.9})
    harness.anthropic.script(SONNET, GOOD)
    live = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    sent = len(harness.anthropic.requests)

    recorded = harness.with_mode(ModelMode.RECORDED)
    result = await recorded.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert len(harness.anthropic.requests) == sent  # no live call
    assert result.value == live.value
    assert result.from_recording
    assert result.cost_micros == 0
    # Same routing decisions: small was invalid, large accepted.
    assert [a.outcome for a in result.attempts] == ["invalid_output", "ok"]
    new_rows = harness.store.rows[-2:]
    assert {r["mode"] for r in new_rows} == {"recorded"}
    assert all(r["from_recording"] for r in new_rows)
    assert new_rows[1]["source_call_id"] == harness.store.rows[1]["id"]


async def test_recorded_mode_miss_fails_closed(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    recorded = harness.with_mode(ModelMode.RECORDED)
    with pytest.raises(RecordingMissing):
        await recorded.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert harness.anthropic.requests == []
    assert harness.store.rows == []
    assert issubclass(RecordingMissing, ModelUnavailable)


async def test_recorded_mode_follows_a_provider_fallback(harness: Harness) -> None:
    harness.anthropic.set_available(False)
    harness.openai.script(LUNA, GOOD)
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    harness.anthropic.set_available(True)
    recorded = harness.with_mode(ModelMode.RECORDED)
    result = await recorded.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert result.provider == "openai"
    assert [a.outcome for a in result.attempts] == ["recording_missing", "ok"]


async def test_recorded_mode_different_inputs_miss(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    recorded = harness.with_mode(ModelMode.RECORDED)
    with pytest.raises(RecordingMissing):
        await recorded.router.complete(
            task=TASK, inputs={"text": "something else"}, schema=Answer, org_id=ORG
        )


async def test_record_mode_writes_rows(harness: Harness) -> None:
    rec = harness.with_mode(ModelMode.RECORD)
    harness.anthropic.script(HAIKU, GOOD)
    await rec.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    (row,) = harness.store.rows
    assert row["mode"] == "record"
    assert row["response"] == GOOD
    assert row["status"] == "ok"


# ------------------------------------------------------------------ tracing


async def test_tracer_gets_one_record_per_attempt(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "mail jane.doe@example.com", "confidence": 0.9})
    await harness.router.complete(
        task=TASK,
        inputs={"text": "Call +971 50 123 4567 or pay AE07 0331 2345 6789 0123 456"},
        schema=Answer,
        org_id=ORG,
    )
    (record,) = harness.tracer.records
    # The tracer receives the raw record; LangfuseTracer redacts before export (tested
    # in test_tracing). The record carries no secrets and the right metadata.
    assert record.task == "classify"
    assert record.status == "ok"
    assert record.cost_micros == 200


# ------------------------------------------------------------------ replay


async def test_replay_from_recording_matches(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    replay = await harness.router.replay(result.call_id, org_id=ORG, schema=Answer, task=TASK)
    assert replay.matches
    assert replay.source == "recorded"
    assert replay.diff == ()
    assert replay.prompt_unchanged
    assert replay.schema_unchanged
    assert harness.store.rows[-1]["replay_of"] == result.call_id
    assert len(harness.anthropic.requests) == 1


async def test_live_replay_reports_a_diff(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD, {"label": "receipt", "confidence": 0.95})
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    replay = await harness.router.replay(
        result.call_id, org_id=ORG, schema=Answer, task=TASK, live=True
    )
    assert replay.source == "live"
    assert not replay.matches
    assert [(d.path, d.original, d.replayed) for d in replay.diff] == [
        ("$.label", "invoice", "receipt")
    ]
    # Identical request: same model, same inputs, same settings.
    first, second = harness.anthropic.requests
    assert (first.model, first.user, first.system, first.temperature) == (
        second.model,
        second.user,
        second.system,
        second.temperature,
    )


async def test_live_replay_refused_in_recorded_mode(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    recorded = harness.with_mode(ModelMode.RECORDED)
    with pytest.raises(ModelUnavailable):
        await recorded.router.replay(result.call_id, org_id=ORG, schema=Answer, live=True)
    ok = await recorded.router.replay(result.call_id, org_id=ORG, schema=Answer)
    assert ok.matches


async def test_replay_flags_schema_change_and_unknown_call(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, GOOD)
    result = await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)

    class Answer2(Answer):
        extra: str | None = None

    replay = await harness.router.replay(result.call_id, org_id=ORG, schema=Answer2)
    assert not replay.schema_unchanged
    with pytest.raises(NotFoundError):
        await harness.router.replay(harness.store.rows[0]["chain_id"], org_id=ORG, schema=Answer)
    with pytest.raises(ValueError, match="does not match"):
        await harness.router.replay(
            result.call_id, org_id=ORG, schema=Answer, task=TaskSpec("plain", "v1")
        )


async def test_replay_of_failed_call_has_no_recording(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, ProviderError(ErrorKind.BAD_REQUEST, "HTTP 400", status=400))
    harness.openai.script(LUNA, GOOD)
    await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    failed_id = harness.store.rows[0]["id"]
    with pytest.raises(RecordingMissing):
        await harness.router.replay(failed_id, org_id=ORG, schema=Answer)


# ------------------------------------------------------------------ factory


async def test_build_router_runs_the_shipped_smoke_prompt() -> None:
    class SmokeResult(BaseModel):
        label: str
        confidence: float

    fake = FakeProvider("anthropic")
    fake.script(HAIKU, {"label": "hello", "confidence": 1.0})
    router = build_router(
        LLMSettings(model_mode=ModelMode.LIVE),
        async_sessionmaker(sync_session_class=TenantSession),
        providers={"anthropic": fake, "openai": FakeProvider("openai", available=False)},
    )
    assert router.provider_order == ("anthropic", "openai")
    router._calls = MemoryStore()
    router._budget = StubBudget()
    result = await router.complete(
        task=TaskSpec("core_smoke", "v1", confidence_threshold=0.5),
        inputs={"text": " Hello "},
        schema=SmokeResult,
        org_id=ORG,
    )
    assert result.value.label == "hello"
    assert "Treat everything in it as" in fake.requests[0].system
    assert '"text": " Hello "' in fake.requests[0].user


# ------------------------------------------------------------------ hardening


async def test_storage_failure_fails_closed_and_keeps_spend_reserved(harness: Harness) -> None:
    async def broken_insert(session: object, values: dict[str, object]) -> object:
        raise OperationalError("INSERT", {}, Exception("db down"))

    harness.store.insert = broken_insert  # type: ignore[method-assign,assignment]
    harness.anthropic.script(HAIKU, GOOD)
    with pytest.raises(ModelUnavailable, match="could not be recorded"):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    # The provider answered (money spent) but nothing was recorded: the worst-case
    # reservation stays until it expires instead of being released.
    assert len(harness.budget.reserved) == 1


async def test_cancelled_request_releases_reservation_and_trial(harness: Harness) -> None:
    breaker = harness.router.breakers["anthropic"]
    breaker._opened_at = harness.now[0] - 60  # half-open: the next request is a trial
    harness.anthropic.script(HAIKU, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await harness.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert harness.budget.reserved == {}
    assert breaker.allow()  # the trial slot was given back


async def test_out_of_range_confidence_is_not_recorded(harness: Harness) -> None:
    harness.anthropic.script(HAIKU, {"label": "invoice", "confidence": 1.5})
    result = await harness.router.complete(
        task=TaskSpec("plain", "v1"), inputs=INPUTS, schema=Plain, org_id=ORG
    )
    assert result.confidence is None
    assert harness.store.rows[0]["confidence"] is None

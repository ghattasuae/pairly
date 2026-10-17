# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Counting PII redactions per organisation (the AI governance board), no database."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from pairly_core.guardrails import redaction_counts
from pairly_core.guardrails.pii import PiiRedactor, redact_log_event
from pairly_core.guardrails.redaction_counts import MAX_KEYS, RedactionTally
from pairly_core.llm.tracing import GenerationRecord, LangfuseTracer

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")


def test_redact_counted_counts_every_masked_value() -> None:
    value = {
        "a": "mail jordan@example.test or +971 50 123 4567",
        "b": ["AE07 0331 2345 6789 0123 456", 3, None],
        "c": ("nothing here",),
    }
    redacted, n = PiiRedactor().redact_counted(value)
    assert n == 3
    assert redacted == PiiRedactor().redact(value)
    assert "[EMAIL]" in redacted["a"]
    assert PiiRedactor().redact_counted("plain text") == ("plain text", 0)


def test_tally_adds_per_org_and_day_and_ignores_unknown_orgs() -> None:
    tally = RedactionTally()
    day = date(2026, 10, 16)
    tally.add(ORG, 2, day=day)
    tally.add(str(ORG), 3, day=day)
    tally.add(None, 5)
    tally.add("not-a-uuid", 5)
    tally.add(ORG, 0)
    assert tally.pending() == 5
    assert tally.drain() == {(ORG, day): 5}
    assert tally.drain() == {}
    tally.restore({(ORG, day): 4})
    assert tally.pending() == 4


def test_tally_is_capped() -> None:
    tally = RedactionTally()
    for i in range(MAX_KEYS + 5):
        tally.add(uuid.UUID(int=i + 1), 1, day=date(2026, 1, 1))
    assert len(tally.drain()) == MAX_KEYS


def test_log_processor_counts_for_the_bound_org(monkeypatch: pytest.MonkeyPatch) -> None:
    tally = RedactionTally()
    monkeypatch.setattr(redaction_counts, "TALLY", tally)
    event: dict[str, Any] = {
        "event": "sent to jordan@example.test",
        "org_id": str(ORG),
        "phone": "+971 50 123 4567",
    }
    out = redact_log_event(None, "info", event)
    assert out["event"] == "sent to [EMAIL]"
    assert out["org_id"] == str(ORG)  # a safe key, kept as is
    assert tally.pending() == 2
    # No organisation (a platform log): redacted, not counted.
    redact_log_event(None, "info", {"event": "jordan@example.test"})
    assert tally.pending() == 2


class _Generation:
    def update(self, **_: Any) -> None:
        return None

    def end(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.inputs: list[Any] = []

    def start_observation(self, **kwargs: Any) -> _Generation:
        self.inputs.append(kwargs["input"])
        return _Generation()


def _record(**overrides: Any) -> GenerationRecord:
    values: dict[str, Any] = {
        "name": "t",
        "chain_id": "c",
        "call_id": None,
        "org_id": str(ORG),
        "task": "t",
        "prompt_version": "v1",
        "prompt_hash": "h",
        "provider": "anthropic",
        "model": "m",
        "tier": "small",
        "status": "ok",
        "input": {"cv": "jordan@example.test"},
        "output": {"summary": "call +971 50 123 4567"},
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_micros": 0,
        "latency_ms": 1,
        "from_recording": True,
    }
    values.update(overrides)
    return GenerationRecord(**values)


def test_tracer_counts_what_it_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    tally = RedactionTally()
    monkeypatch.setattr(redaction_counts, "TALLY", tally)
    client = _Client()
    LangfuseTracer(client, PiiRedactor()).record_generation(_record())
    assert client.inputs == [{"cv": "[EMAIL]"}]
    assert tally.pending() == 2
    assert {org for org, _ in tally.drain()} == {ORG}


class _Broken:
    def __call__(self) -> Any:
        raise ConnectionError("database down")


async def test_flush_puts_counts_back_when_the_database_fails() -> None:
    tally = RedactionTally()
    tally.add(ORG, 3)
    result = await redaction_counts.flush(_Broken(), tally)  # type: ignore[arg-type]
    assert (result.rows, result.redactions, result.failed_orgs) == (0, 0, 1)
    assert tally.pending() == 3

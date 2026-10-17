# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""WP1.12 without a database: flag, job, retention and search registries, job summaries,
the ``tracked`` wrapper and PII redaction of search chunks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pairly_core.flags.registry import (
    CORE_FLAGS,
    SEARCH_DOCUMENTS,
    WEBHOOKS_OUTBOUND,
    FlagError,
    FlagRegistry,
    FlagSpec,
    build_flag_registry,
)
from pairly_core.jobs.registry import (
    CORE_JOBS,
    JobRegistry,
    JobRegistryError,
    JobSpec,
    build_job_registry,
)
from pairly_core.jobs.runs import clean_summary, tracked
from pairly_core.retention.rules import (
    CORE_RULES,
    RetentionRegistry,
    RetentionRule,
    RetentionRuleError,
    build_retention_registry,
)
from pairly_core.retrieval.search import ChunkInput, SearchableKind
from pairly_core.search.documents import redact_chunks
from pairly_core.search.sources import (
    CORE_SOURCES,
    SearchRegistry,
    SearchSource,
    SearchSourceError,
    build_search_registry,
)
from pairly_core.webhooks.models import InboundEvent

# ------------------------------------------------------------------ flags


def test_flag_registry() -> None:
    registry = FlagRegistry()
    assert [f.key for f in registry.flags] == sorted(f.key for f in CORE_FLAGS)
    assert registry.require(WEBHOOKS_OUTBOUND).default is True
    # Copying document text into the index is opt in.
    assert registry.require(SEARCH_DOCUMENTS).default is False
    with pytest.raises(FlagError, match="unknown"):
        registry.require("nope.nope")
    with pytest.raises(FlagError, match="duplicate"):
        registry.register(CORE_FLAGS[0])
    for bad in ("Bad.Key", "single", "a.b.c.d.e"):
        with pytest.raises(FlagError):
            FlagSpec(bad, "x")


def test_plugin_flags_stay_in_their_namespace() -> None:
    class Pay:
        id = "pay"
        feature_flags = (FlagSpec("pay.auto_match", "Auto match invoices"),)

    class Rogue:
        id = "hire"
        feature_flags = (FlagSpec("webhooks.other", "Outside"),)

    class Wrong:
        id = "pay"
        feature_flags = ("pay.thing",)

    assert "pay.auto_match" in build_flag_registry([Pay()])
    with pytest.raises(FlagError, match="namespace"):
        build_flag_registry([Rogue()])
    with pytest.raises(FlagError, match="FlagSpec"):
        build_flag_registry([Wrong()])


# ------------------------------------------------------------------ jobs


def test_job_registry() -> None:
    registry = JobRegistry()
    assert len(registry.jobs) == len(CORE_JOBS)
    with pytest.raises(JobRegistryError, match="unknown"):
        registry.require("nothing")
    with pytest.raises(JobRegistryError, match="duplicate"):
        registry.register(CORE_JOBS[0])
    with pytest.raises(JobRegistryError):
        JobSpec("Bad Name", "x", "daily")
    with pytest.raises(JobRegistryError):
        JobSpec("fine", "", "daily")

    class Hire:
        id = "hire"
        scheduled_jobs = (JobSpec("hire_sla_check", "SLA check", "hourly"),)

    class Rogue:
        id = "hire"
        scheduled_jobs = (JobSpec("pay_sweep", "Outside", "hourly"),)

    assert "hire_sla_check" in build_job_registry([Hire()])
    with pytest.raises(JobRegistryError, match="namespace"):
        build_job_registry([Rogue()])


def test_clean_summary_keeps_only_small_facts() -> None:
    summary = clean_summary(
        {
            "orgs": 3,
            "ok": True,
            "broken": ["org-a", "org-b"],  # ids become a count
            "note": "x" * 500,
            "nested": {"deep": {"deeper": {"x": 1}}},
            "when": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert summary is not None
    assert summary["orgs"] == 3
    assert summary["ok"] is True
    assert summary["broken"] == 2
    assert len(summary["note"]) == 100
    # Two levels of mappings at most; anything deeper is reduced to its type name.
    assert summary["nested"] == {"deep": "dict"}
    assert summary["when"] == "datetime"
    assert clean_summary(None) is None
    assert clean_summary([1, 2]) is None
    huge = clean_summary({f"k{i}": "v" * 100 for i in range(200)})
    assert huge is not None
    assert len(huge) <= 50


async def test_tracked_without_a_database_still_runs_the_job() -> None:
    calls: list[dict[str, Any]] = []

    async def job(ctx: dict[str, Any]) -> dict[str, Any]:
        calls.append(ctx)
        return {"done": 1}

    async def broken(ctx: dict[str, Any]) -> None:
        raise ValueError("boom")

    wrapped = tracked("unit_job", job)
    assert wrapped.__wrapped__ is job  # type: ignore[attr-defined]
    assert await wrapped({}) == {"done": 1}
    assert len(calls) == 1
    with pytest.raises(ValueError, match="boom"):
        await tracked("unit_job", broken)({})
    with pytest.raises(JobRegistryError):
        tracked("Not Valid", job)


# ------------------------------------------------------------------ retention


def test_retention_rules() -> None:
    registry = RetentionRegistry()
    assert [r.key for r in registry.rules] == sorted(r.key for r in CORE_RULES)
    now = datetime(2026, 10, 7, tzinfo=UTC)
    rule = next(r for r in CORE_RULES if r.key == "inbound_events.seen")
    assert rule.cutoff(now) == now - timedelta(days=90)
    assert len(rule.due(now)) == 2
    with pytest.raises(RetentionRuleError):
        RetentionRule("Bad Key", "x", InboundEvent, "received_at", 1)
    with pytest.raises(RetentionRuleError):
        RetentionRule("x.y", "x", InboundEvent, "received_at", -1)
    with pytest.raises(RetentionRuleError):
        RetentionRule("x.y", "x", InboundEvent, "no_such_column", 1)
    with pytest.raises(RetentionRuleError, match="duplicate"):
        RetentionRegistry([*CORE_RULES, CORE_RULES[0]])

    class Hire:
        id = "hire"
        retention_rules = (RetentionRule("hire.events", "x", InboundEvent, "received_at", 365),)

    class Rogue:
        id = "hire"
        retention_rules = (RetentionRule("pay.events", "x", InboundEvent, "received_at", 1),)

    assert "hire.events" in build_retention_registry([Hire()])
    with pytest.raises(RetentionRuleError, match="namespace"):
        build_retention_registry([Rogue()])


# ------------------------------------------------------------------ search


def test_search_registry() -> None:
    registry = SearchRegistry()
    assert [s.kind.key for s in registry.sources] == ["document", "policy"]
    assert registry.get("document") is not None
    assert registry.get("nothing") is None
    with pytest.raises(SearchSourceError, match="duplicate"):
        SearchRegistry([*CORE_SOURCES, CORE_SOURCES[0]])
    base = CORE_SOURCES[0]

    class Hire:
        id = "hire"
        search_sources = (
            SearchSource(
                SearchableKind("hire_candidate"), base.may_search, base.scope, base.describe
            ),
        )

    class Rogue:
        id = "hire"
        search_sources = (
            SearchSource(SearchableKind("pay_invoice"), base.may_search, base.scope, base.describe),
        )

    assert build_search_registry([Hire()]).get("hire_candidate") is not None
    with pytest.raises(SearchSourceError, match="namespace"):
        build_search_registry([Rogue()])


def test_search_chunks_are_redacted() -> None:
    chunks = [
        ChunkInput(
            text="Pay to AE07 0331 2345 6789 0123 456, questions to ap@vendor.example or "
            "+971 50 123 4567.",
            page_start=1,
            page_end=1,
            section="Bank details for ap@vendor.example",
            meta={"k": "v"},
        ),
        ChunkInput(text="ap@vendor.example"),  # nothing but PII: kept as a placeholder
        ChunkInput(text="   "),
    ]
    out = redact_chunks(chunks)
    assert len(out) == 2
    first = out[0]
    assert "AE07" not in first.text
    assert "ap@vendor.example" not in first.text
    assert "123 4567" not in first.text
    assert "[IBAN]" in first.text
    assert "[EMAIL]" in first.text
    assert first.section == "Bank details for [EMAIL]"
    assert first.meta == {"k": "v"}
    assert (first.page_start, first.page_end) == (1, 1)
    assert out[1].text == "[EMAIL]"


def test_plugin_jobs_with_a_coroutine() -> None:
    async def run(ctx: Any) -> dict[str, Any]:
        return {}

    job = JobSpec("pay_sweep", "Sweep", "every 5 minutes", run=run, every_minutes=5)
    assert job.minutes == set(range(0, 60, 5))
    assert JobSpec("pay_x", "x", "daily").minutes == set()
    for every in (0, 7, 61):
        with pytest.raises(JobRegistryError):
            JobSpec("pay_sweep", "Sweep", "x", run=run, every_minutes=every)
    with pytest.raises(JobRegistryError):
        JobSpec("pay_sweep", "Sweep", "x", run=run)

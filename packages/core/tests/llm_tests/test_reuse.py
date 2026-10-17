# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Live mode response reuse: an identical request of the same organisation reuses the
first live answer (same output, no model request, no cost), labelled ``reused_from``;
per organisation, within the TTL, with an off switch."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.settings import ModelMode
from pairly_core.llm.store import (
    OUTPUT_STATUSES,
    REUSABLE_MODES,
    DatabaseCallStore,
    ReusableCallStore,
    StoredCall,
)
from pairly_core.llm.types import TaskSpec

from .conftest import HAIKU, ORG, SONNET, Answer, MemoryStore, make_harness

TASK = TaskSpec(name="classify", prompt_version="v1", confidence_threshold=0.7)
INPUTS = {"text": "Invoice from ACME"}
GOOD = {"label": "invoice", "confidence": 0.95}
OTHER_ORG = uuid.UUID("00000000-0000-4000-8000-000000000002")


class ReusableStore(MemoryStore):
    """``MemoryStore`` plus ``find_reusable`` with the database store's rules."""

    def __init__(self) -> None:
        super().__init__()
        self.now = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC)

    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
        values.setdefault("created_at", self.now)
        return await super().insert(session, values)

    async def find_reusable(
        self, session: AsyncSession, request_hash: str, *, org_id: uuid.UUID, since: dt.datetime
    ) -> StoredCall | None:
        statuses = {s.value for s in OUTPUT_STATUSES}
        for row in self.rows:
            if (
                row["org_id"] == org_id
                and row["request_hash"] == request_hash
                and row["mode"] in REUSABLE_MODES
                and row["status"] in statuses
                and not row.get("from_recording")
                and row.get("reused_from") is None
                and row.get("replay_of") is None
                and row.get("response") is not None
                and row["created_at"] >= since
            ):
                return self._stored(row)
        return None


def _harness(prompt_root: Path, store: ReusableStore, **settings: Any) -> Any:
    return make_harness(PromptRegistry([prompt_root]), store=store, **settings)


async def test_identical_request_reuses_the_first_answer(prompt_root: Path) -> None:
    store = ReusableStore()
    assert isinstance(store, ReusableCallStore)
    h = _harness(prompt_root, store)
    h.anthropic.script(HAIKU, GOOD)
    first = await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    second = await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert h.anthropic.calls_for(HAIKU) == 1
    assert second.value == first.value
    assert second.request_hash == first.request_hash
    assert second.cost_micros == 0
    assert second.total_cost_micros == 0
    assert not second.from_recording
    original, reused = store.rows
    assert reused["reused_from"] == original["id"] == first.call_id
    assert reused["mode"] == "live"
    assert reused["cost_micros"] == 0
    assert reused["response"] == original["response"]
    assert h.budget.reservations == [h.budget.reservations[0]]  # one reservation only
    # A third identical request reuses the original, never the reuse.
    third = await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert store.rows[-1]["reused_from"] == original["id"]
    assert third.value == first.value


async def test_reuse_is_per_organisation_and_per_request(prompt_root: Path) -> None:
    store = ReusableStore()
    h = _harness(prompt_root, store)
    h.anthropic.script(HAIKU, GOOD)
    await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=OTHER_ORG)
    await h.router.complete(task=TASK, inputs={"text": "Other"}, schema=Answer, org_id=ORG)
    assert h.anthropic.calls_for(HAIKU) == 3
    assert all(r.get("reused_from") is None for r in store.rows)


async def test_off_switch_and_ttl(prompt_root: Path) -> None:
    store = ReusableStore()
    off = _harness(prompt_root, store, llm_reuse_responses=False)
    off.anthropic.script(HAIKU, GOOD)
    await off.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    await off.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert off.anthropic.calls_for(HAIKU) == 2

    aged = ReusableStore()
    h = _harness(prompt_root, aged, llm_reuse_ttl_days=7)
    h.anthropic.script(HAIKU, GOOD)
    await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    aged.rows[0]["created_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(days=8)
    await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert h.anthropic.calls_for(HAIKU) == 2
    assert aged.rows[-1].get("reused_from") is None


async def test_an_escalated_chain_is_reused_step_by_step(prompt_root: Path) -> None:
    store = ReusableStore()
    h = _harness(prompt_root, store)
    h.anthropic.script(HAIKU, {"label": "x" * 100, "confidence": 0.9})  # invalid
    h.anthropic.script(SONNET, GOOD)
    first = await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    second = await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert (h.anthropic.calls_for(HAIKU), h.anthropic.calls_for(SONNET)) == (1, 1)
    assert [a.outcome for a in second.attempts] == ["invalid_output", "ok"]
    assert (second.model, second.value) == (first.model, first.value)
    assert [r.get("reused_from") is not None for r in store.rows] == [False, False, True, True]


async def test_no_reuse_outside_live_mode(prompt_root: Path) -> None:
    store = ReusableStore()
    h = _harness(prompt_root, store)
    h.anthropic.script(HAIKU, GOOD)
    await h.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    record = make_harness(
        PromptRegistry([prompt_root]), mode=ModelMode.RECORD, store=store, anthropic=h.anthropic
    )
    await record.router.complete(task=TASK, inputs=INPUTS, schema=Answer, org_id=ORG)
    assert h.anthropic.calls_for(HAIKU) == 2
    assert all(r.get("reused_from") is None for r in store.rows)


async def test_database_lookup_filters() -> None:
    captured: list[Any] = []

    class Session:
        async def scalar(self, statement: Any) -> None:
            captured.append(statement)

    since = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)
    found = await DatabaseCallStore().find_reusable(
        Session(),  # type: ignore[arg-type]
        "h" * 64,
        org_id=ORG,
        since=since,
    )
    assert found is None
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    for part in (
        "model_calls.org_id = ",
        "model_calls.request_hash = ",
        "model_calls.mode IN ",
        "model_calls.status IN ",
        "model_calls.from_recording IS false",
        "model_calls.reused_from IS NULL",
        "model_calls.replay_of IS NULL",
        "model_calls.erased_at IS NULL",
        "model_calls.response IS NOT NULL",
        "model_calls.created_at >= ",
        "ORDER BY model_calls.created_at, model_calls.id",
    ):
        assert part in sql, part

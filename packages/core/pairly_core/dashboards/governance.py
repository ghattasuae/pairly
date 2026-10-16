# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The AI governance dashboard (brief 5.1 item 14), for admins and auditors.

* **Action needed**: the eval gate failing, the budget past its alert threshold.
* **Model usage**: model calls of the last 30 days by component (the router task) and
  workflow: calls, cost, tokens, mean latency, how many came from recordings.
* **Cost and budget**: this month's cost against the monthly budget.
* **Evals**: every result file the gate reads (recorded and, when there was one, live),
  each with its time and its PASS / FAIL / PENDING / INFO counts, and the failing
  metrics. Read from the committed files (``pairly_core.dashboards.evals``).
* **Overrides**: per workflow, AI records of the last 90 days and how many a person
  superseded (the human override rate).
* **Versions in production**: the model and prompt versions the calls of the last 30
  days used, with where their model cards live (``docs/model-cards/``).
* **Guardrails**: PII redaction in logs and traces is not counted anywhere, so it says
  so; the workflows add their injection and hidden-text findings, and Hire its fairness
  runs (the counterfactual runner) and calibration agreement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from pairly_core.dashboards.admin import AdminInputs, budget_section
from pairly_core.dashboards.evals import EvalSummary, read_evals
from pairly_core.dashboards.model import (
    Board,
    Dashboard,
    Item,
    Link,
    Section,
    SectionContext,
    SectionRegistry,
    Stat,
    Unit,
    plugin_sections,
    section,
)
from pairly_core.decisions.models import HUMAN_KINDS, DecisionRecord
from pairly_core.decisions.scope import read_scope
from pairly_core.models import ModelCall
from pairly_core.rbac import Actor

USAGE_DAYS: Final = 30
OVERRIDE_DAYS: Final = 90
MICROS: Final = 1_000_000
MODEL_CARDS: Final = "docs/model-cards/"


@dataclass(frozen=True, slots=True)
class GovernanceInputs:
    admin: AdminInputs
    evals_dir: Path | None


async def _usage(session: AsyncSession, now: datetime) -> Section:
    since = now - timedelta(days=USAGE_DAYS)
    rows = (
        await session.execute(
            select(
                ModelCall.task,
                ModelCall.workflow,
                func.count(),
                func.coalesce(func.sum(ModelCall.cost_micros), 0),
                func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0),
                func.avg(ModelCall.latency_ms),
                func.count().filter(ModelCall.from_recording.is_(True)),
                func.count().filter(ModelCall.status != "ok"),
            )
            .where(ModelCall.created_at >= since)
            .group_by(ModelCall.task, ModelCall.workflow)
            .order_by(func.count().desc())
        )
    ).all()
    total = sum(int(r[2]) for r in rows)
    return Section(
        key="model_usage",
        count=total,
        items=[
            Item(
                kind="component",
                id=f"{task}:{workflow or 'core'}",
                label=task,
                href=f"/audit?workflow={workflow}" if workflow else None,
                detail={
                    "workflow": workflow,
                    "calls": int(calls),
                    "cost_usd": int(cost) / MICROS,
                    "tokens": int(tokens),
                    "mean_latency_ms": round(float(latency), 1) if latency is not None else None,
                    "from_recording": int(recorded),
                    "not_ok": int(failed),
                },
            )
            for task, workflow, calls, cost, tokens, latency, recorded, failed in rows[:20]
        ],
        source="model_calls",
        note=None if rows else "no_data",
    )


def _evals_section(summary: EvalSummary) -> Section:
    if not summary.available:
        return Section(key="evals", count=None, note=summary.reason, source="eval_result_files")
    items: list[Item] = []
    for f in summary.files:
        counts = f.counts()
        items.append(
            Item(
                kind="eval_file",
                id=f.name,
                label=f.name,
                at=f.at,
                status=("missing" if not f.present else ("fail" if counts["FAIL"] else "pass")),
                detail={
                    "mode": f.mode,
                    "time_source": f.at_source,
                    "pass": counts["PASS"],
                    "fail": counts["FAIL"],
                    "pending": counts["PENDING"],
                    "info": counts["INFO"],
                },
            )
        )
    failing = [v for f in summary.files for v in f.verdicts if v.status == "FAIL"]
    for v in failing[:10]:
        items.append(
            Item(
                kind="eval_metric",
                id=f"{v.file}:{v.id}",
                label=v.id,
                status="fail",
                detail={
                    "file": v.file,
                    "value": v.value if isinstance(v.value, int | float | str | bool) else None,
                    "threshold": v.threshold,
                    "why": v.detail or None,
                },
            )
        )
    recorded, live = summary.gate("recorded"), summary.gate("live")
    return Section(
        key="evals",
        count=len(failing),
        items=items,
        stats=[
            Stat("gate_recorded", None, Unit.TEXT, text=recorded),
            Stat("gate_live", None, Unit.TEXT, text=live),
            Stat(
                "metrics_pending_live",
                float(sum(1 for f in summary.files for v in f.verdicts if v.status == "PENDING")),
            ),
        ],
        source="eval_result_files",
        note=None if summary.files else "no_data",
    )


async def _overrides(session: AsyncSession, actor: Actor, now: datetime) -> Section:
    scope = read_scope(actor, "core.decision.read")
    since = now - timedelta(days=OVERRIDE_DAYS)
    human = [k.value for k in HUMAN_KINDS]
    later = aliased(DecisionRecord)
    rows = (
        await session.execute(
            select(DecisionRecord.workflow, func.count(), func.count(later.id))
            .outerjoin(
                later,
                and_(
                    later.org_id == DecisionRecord.org_id,
                    later.supersedes == DecisionRecord.id,
                    later.kind.in_(human),
                ),
            )
            .where(
                DecisionRecord.kind.not_in(human),
                DecisionRecord.decided_at >= since,
                scope.clause(DecisionRecord.workflow),
            )
            .group_by(DecisionRecord.workflow)
            .order_by(DecisionRecord.workflow)
        )
    ).all()
    stats = [
        Stat(
            f"override_rate_{workflow}",
            (int(overridden) / int(total)) if int(total) else None,
            Unit.RATIO,
            n=int(total),
        )
        for workflow, total, overridden in rows
    ]
    return Section(
        key="human_overrides",
        count=sum(int(r[2]) for r in rows),
        items=[
            Item(
                kind="workflow",
                id=str(workflow),
                label=str(workflow),
                href=f"/audit?workflow={workflow}&action=core.decision.recorded",
                detail={"ai_records": int(total), "overridden": int(overridden)},
            )
            for workflow, total, overridden in rows
        ],
        stats=stats,
        source="decision_records",
        note=None if rows else "no_data",
    )


async def _versions(session: AsyncSession, now: datetime) -> Section:
    since = now - timedelta(days=USAGE_DAYS)
    rows = (
        await session.execute(
            select(
                ModelCall.task,
                ModelCall.prompt_version,
                ModelCall.provider,
                ModelCall.model,
                ModelCall.mode,
                func.count(),
                func.max(ModelCall.created_at),
            )
            .where(ModelCall.created_at >= since)
            .group_by(
                ModelCall.task,
                ModelCall.prompt_version,
                ModelCall.provider,
                ModelCall.model,
                ModelCall.mode,
            )
            .order_by(ModelCall.task, func.max(ModelCall.created_at).desc())
        )
    ).all()
    return section(
        "versions_in_production",
        [
            Item(
                kind="model_version",
                id=f"{task}@{prompt}:{provider}/{model}:{mode}",
                label=task,
                at=last,
                detail={
                    "prompt_version": prompt,
                    "provider": provider,
                    "model": model,
                    "mode": mode,
                    "calls": int(calls),
                    "model_card": MODEL_CARDS,
                },
            )
            for task, prompt, provider, model, mode, calls, last in rows
        ],
        source="model_calls",
        note=None if rows else "no_data",
    )


def _redactions() -> Section:
    # The router and the log processors redact personal data before anything is logged
    # or traced, but no count of it is kept: say so rather than show a number.
    return Section(
        key="pii_redactions",
        count=None,
        stats=[Stat("pii_redactions", None)],
        note="not_recorded",
    )


async def governance_dashboard(
    session: AsyncSession,
    actor: Actor,
    *,
    now: datetime,
    sections: SectionRegistry,
    inputs: GovernanceInputs,
) -> Dashboard:
    evals = _evals_section(read_evals(inputs.evals_dir))
    budget, alert = await budget_section(session, actor, inputs.admin, now)
    extra_action, extra = await plugin_sections(
        sections, Board.AI_GOVERNANCE, SectionContext(session=session, actor=actor, now=now)
    )
    action: list[Section] = []
    gate = next((s.text for s in evals.stats if s.key == "gate_recorded"), None)
    live = next((s.text for s in evals.stats if s.key == "gate_live"), None)
    if "fail" in (gate, live):
        action.append(
            Section(
                key="eval_gate_failing",
                count=evals.count,
                items=[i for i in evals.items if i.kind == "eval_metric"],
                source=evals.source,
            )
        )
    if alert:
        action.append(
            Section(key="budget_alert", count=1, stats=budget.stats, note="over_threshold")
        )
    action.extend(extra_action)
    return Dashboard(
        board=Board.AI_GOVERNANCE,
        generated_at=now,
        action=action,
        sections=[
            evals,
            await _usage(session, now),
            budget,
            await _overrides(session, actor, now),
            await _versions(session, now),
            _redactions(),
            *extra,
        ],
        links=[Link("model_cards", MODEL_CARDS)],
    )

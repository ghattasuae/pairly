# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The expense workflow plugin, registered in the ``pairly.workflows`` entry point group.

Everything the engine needs to run expense notes, declared on one object
(``pairly_core.plugins.WorkflowPlugin`` plus optional attributes):

* ``permissions``  ``expense.*`` with default role grants (``permissions``).
* ``graphs``       the ``expense_note`` graph, its ``expense.note`` approval type (default
                   rules in ``policy/approval_rules.yaml``) and its one final action,
                   ``mark_reimbursable``.
* ``webhook_events``  ``expense.note.decided`` for subscribers.
* ``api_router``   the ``/v1/expense`` routes (imported only when called, so the worker
                   never loads them).
* ``demo_seeds``   ``expense-demo`` for ``pairly-admin seed``.
* ``migrations_dir``  the ``expense`` Alembic branch (``migrations/``).
* ``scheduled_jobs``  ``expense_stuck_note_sweep``, run by the worker every 5 minutes.

Importing this module also registers the ``expense_notes`` table on ``Base.metadata``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pairly_core.jobs.registry import JobSpec
from pairly_core.plugins import DemoSeed
from pairly_core.rbac import PermissionSpec
from pairly_core.webhooks.events import EventType
from pairly_core.workflow.spec import ApprovalType, GraphSpec
from pairly_expense import models  # noqa: F401  (registers expense_notes)
from pairly_expense.actions import mark_reimbursable
from pairly_expense.graph import DECIDED_EVENT, FINAL_ACTION, GRAPH, VERSION, build_graph
from pairly_expense.permissions import NOTE_APPROVE, NOTE_READ, NOTE_SUBMIT, PERMISSIONS
from pairly_expense.policies import APPROVAL_RULES_V1, APPROVAL_TYPE
from pairly_expense.seed import seed_expense_demo
from pairly_expense.sweeper import JOB_NAME, sweep_stuck_notes

if TYPE_CHECKING:
    from fastapi import APIRouter

EXPENSE_NOTE: GraphSpec = GraphSpec(
    workflow="expense",
    name=GRAPH,
    version=VERSION,
    build=build_graph,
    start_permission=NOTE_SUBMIT,
    read_permission=NOTE_READ,
    approval_types=(ApprovalType(APPROVAL_TYPE, NOTE_APPROVE, APPROVAL_RULES_V1, "Expense note"),),
    final_actions={FINAL_ACTION: mark_reimbursable},
    description="Receipt to reimbursable: read, check, and a person decides.",
)

SCHEDULED_JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        JOB_NAME,
        "Restart expense runs that never started; send stuck notes to a person",
        "every 5 minutes",
        run=sweep_stuck_notes,
        every_minutes=5,
    ),
)

WEBHOOK_EVENTS: tuple[EventType, ...] = (
    EventType(DECIDED_EVENT, "A person decided an expense note"),
)


def api_router() -> APIRouter:
    from pairly_expense.api import router

    return router


@dataclass(frozen=True, slots=True)
class ExpensePlugin:
    id: str = "expense"
    name: str = "Pairly Expense"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)
    graphs: tuple[GraphSpec, ...] = (EXPENSE_NOTE,)
    webhook_events: tuple[EventType, ...] = WEBHOOK_EVENTS
    demo_seeds: Mapping[str, DemoSeed] = field(
        default_factory=lambda: {"expense-demo": seed_expense_demo}
    )
    migrations_dir: Path = Path(__file__).resolve().parent / "migrations"
    scheduled_jobs: tuple[JobSpec, ...] = SCHEDULED_JOBS

    def api_router(self) -> APIRouter:
        return api_router()


plugin = ExpensePlugin()

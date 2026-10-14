# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pairly Pay plugin, registered in the ``pairly.workflows`` entry point group.

What Pay declares to the engine (``pairly_core.plugins.WorkflowPlugin`` plus optional
attributes):

* ``permissions``     ``pay.*`` with default role grants (``permissions``).
* ``graphs``          ``pay_bank_change``: a verified bank-detail change waits for its
                      dual approval (approval type ``pay.bank_change``, default rules in
                      ``policy/bank_change_approval_rules.yaml``), with its one final
                      action ``approve_bank_change``.
* ``api_router``      the ``/v1/pay`` routes (imported only when called, so the worker
                      never loads them).
* ``migrations_dir``  the ``pay`` Alembic branch (``migrations/``).
* ``scheduled_jobs``  ``pay_bank_change_activation``, every 5 minutes: new details go
                      live once their cooling-off period is over.

Importing this module also registers the Pay tables on ``Base.metadata``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pairly_core.jobs.registry import JobSpec
from pairly_core.rbac import PermissionSpec
from pairly_core.workflow.spec import ApprovalType, GraphSpec
from pairly_pay import models  # noqa: F401  (registers the Pay tables)
from pairly_pay.bank_changes import FINAL_ACTION, approve_bank_change
from pairly_pay.graph import APPROVAL_TYPE, GRAPH, VERSION, build_graph
from pairly_pay.jobs import ACTIVATION_JOB, activate_due_changes
from pairly_pay.permissions import (
    BANK_CHANGE_APPROVE,
    BANK_CHANGE_CREATE,
    BANK_CHANGE_READ,
    PERMISSIONS,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

BANK_CHANGE_RULES_V1: Final = (
    files("pairly_pay")
    .joinpath("policy", "bank_change_approval_rules.yaml")
    .read_text(encoding="utf-8")
)

PAY_BANK_CHANGE: GraphSpec = GraphSpec(
    workflow="pay",
    name=GRAPH,
    version=VERSION,
    build=build_graph,
    start_permission=BANK_CHANGE_CREATE,
    read_permission=BANK_CHANGE_READ,
    approval_types=(
        ApprovalType(
            APPROVAL_TYPE, BANK_CHANGE_APPROVE, BANK_CHANGE_RULES_V1, "Vendor bank-detail change"
        ),
    ),
    final_actions={FINAL_ACTION: approve_bank_change},
    description="A verified change of vendor bank details waits for two approvers.",
)

SCHEDULED_JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        ACTIVATION_JOB,
        "Activate approved vendor bank details once their cooling-off period is over",
        "every 5 minutes",
        run=activate_due_changes,
        every_minutes=5,
    ),
)


def api_router() -> APIRouter:
    from pairly_pay.api import router

    return router


@dataclass(frozen=True, slots=True)
class PayPlugin:
    id: str = "pay"
    name: str = "Pairly Pay"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)
    graphs: tuple[GraphSpec, ...] = (PAY_BANK_CHANGE,)
    migrations_dir: Path = Path(__file__).resolve().parent / "migrations"
    scheduled_jobs: tuple[JobSpec, ...] = SCHEDULED_JOBS

    def api_router(self) -> APIRouter:
        return api_router()


plugin = PayPlugin()

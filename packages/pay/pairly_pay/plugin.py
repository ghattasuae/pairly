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
                      ``pay_invoice``: intake of one invoice (scan, parse, guardrails,
                      extraction or e-invoice reading, vendor match) up to ``ready`` or
                      ``needs_review``. No approval and no final action: it never
                      approves or pays anything.
                      ``pay_invoice_approval``: an invoice a person sent for approval
                      waits for its approvers (approval type ``pay.invoice``, rules in
                      ``policy/invoice_approval_rules.yaml``), final action
                      ``approve_invoice``.
                      ``pay_payment_run``: a proposed payment run waits for a finance
                      controller (approval type ``pay.payment_run``), final action
                      ``approve_payment_run``. No bank connection: the run ends in a
                      file a person downloads.
* ``api_router``      the ``/v1/pay`` routes (imported only when called, so the worker
                      never loads them).
* ``migrations_dir``  the ``pay`` Alembic branch (``migrations/``).
* ``scheduled_jobs``  ``pay_bank_change_activation``, every 5 minutes: new details go
                      live once their cooling-off period is over.
                      ``pay_invoice_intake_start``, every minute: start reading received
                      invoices (email intake), and send stuck ones to a person.
                      ``pay_inbound_drop``, every minute, development and test only:
                      deliver the ``.eml`` files dropped in ``PAIRLY_INBOUND_DROP_DIR``
                      (``pairly_pay.inbound_drop``).

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
from pairly_pay.approval_graph import build_graph as build_approval_graph
from pairly_pay.approvals import APPROVAL_TYPE as INVOICE_APPROVAL_TYPE
from pairly_pay.approvals import FINAL_ACTION as INVOICE_FINAL_ACTION
from pairly_pay.approvals import GRAPH as APPROVAL_GRAPH
from pairly_pay.approvals import INVOICE_RULES_V1, approve_invoice
from pairly_pay.approvals import VERSION as APPROVAL_GRAPH_VERSION
from pairly_pay.bank_changes import FINAL_ACTION, approve_bank_change
from pairly_pay.graph import APPROVAL_TYPE, GRAPH, VERSION, build_graph
from pairly_pay.inbound_drop import DROP_JOB
from pairly_pay.invoice_graph import GRAPH as INVOICE_GRAPH
from pairly_pay.invoice_graph import VERSION as INVOICE_GRAPH_VERSION
from pairly_pay.invoice_graph import build_graph as build_invoice_graph
from pairly_pay.invoice_start import INTAKE_JOB
from pairly_pay.jobs import ACTIVATION_JOB, activate_due_changes
from pairly_pay.payment_run_graph import build_graph as build_payment_run_graph
from pairly_pay.payment_runs import APPROVAL_TYPE as RUN_APPROVAL_TYPE
from pairly_pay.payment_runs import FINAL_ACTION as RUN_FINAL_ACTION
from pairly_pay.payment_runs import GRAPH as RUN_GRAPH
from pairly_pay.payment_runs import PAYMENT_RUN_RULES_V1, approve_payment_run
from pairly_pay.payment_runs import VERSION as RUN_GRAPH_VERSION
from pairly_pay.permissions import (
    BANK_CHANGE_APPROVE,
    BANK_CHANGE_CREATE,
    BANK_CHANGE_READ,
    INVOICE_APPROVE,
    INVOICE_PROPOSE,
    INVOICE_READ,
    INVOICE_SUBMIT,
    PAYMENT_RUN_APPROVE,
    PAYMENT_RUN_CREATE,
    PAYMENT_RUN_READ,
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

PAY_INVOICE: GraphSpec = GraphSpec(
    workflow="pay",
    name=INVOICE_GRAPH,
    version=INVOICE_GRAPH_VERSION,
    build=build_invoice_graph,
    start_permission=INVOICE_PROPOSE,
    read_permission=INVOICE_READ,
    description="Read one received invoice and stop at ready or needs review.",
)

PAY_INVOICE_APPROVAL: GraphSpec = GraphSpec(
    workflow="pay",
    name=APPROVAL_GRAPH,
    version=APPROVAL_GRAPH_VERSION,
    build=build_approval_graph,
    start_permission=INVOICE_SUBMIT,
    read_permission=INVOICE_READ,
    approval_types=(
        ApprovalType(INVOICE_APPROVAL_TYPE, INVOICE_APPROVE, INVOICE_RULES_V1, "Invoice approval"),
    ),
    final_actions={INVOICE_FINAL_ACTION: approve_invoice},
    description="An invoice sent for approval waits for the approvers its rules pick.",
)

PAY_PAYMENT_RUN: GraphSpec = GraphSpec(
    workflow="pay",
    name=RUN_GRAPH,
    version=RUN_GRAPH_VERSION,
    build=build_payment_run_graph,
    start_permission=PAYMENT_RUN_CREATE,
    read_permission=PAYMENT_RUN_READ,
    approval_types=(
        ApprovalType(RUN_APPROVAL_TYPE, PAYMENT_RUN_APPROVE, PAYMENT_RUN_RULES_V1, "Payment run"),
    ),
    final_actions={RUN_FINAL_ACTION: approve_payment_run},
    description="A proposed payment run waits for a finance controller.",
)

SCHEDULED_JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        ACTIVATION_JOB,
        "Activate approved vendor bank details once their cooling-off period is over",
        "every 5 minutes",
        run=activate_due_changes,
        every_minutes=5,
    ),
    INTAKE_JOB,
    DROP_JOB,
)


def api_router() -> APIRouter:
    from pairly_pay.api import router

    return router


@dataclass(frozen=True, slots=True)
class PayPlugin:
    id: str = "pay"
    name: str = "Pairly Pay"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)
    graphs: tuple[GraphSpec, ...] = (
        PAY_BANK_CHANGE,
        PAY_INVOICE,
        PAY_INVOICE_APPROVAL,
        PAY_PAYMENT_RUN,
    )
    migrations_dir: Path = Path(__file__).resolve().parent / "migrations"
    scheduled_jobs: tuple[JobSpec, ...] = SCHEDULED_JOBS

    def api_router(self) -> APIRouter:
        return api_router()


plugin = PayPlugin()

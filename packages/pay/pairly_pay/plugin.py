# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Placeholder Pairly Pay plugin, registered in the ``pairly.workflows`` entry point group.

The full plugin (models, graphs, detectors, rules, router) is built in later work packages.
"""

from dataclasses import dataclass, field

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec
from pairly_core.workflow.spec import GraphSpec

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        "pay.invoice.read",
        "View invoices",
        frozenset({R.ADMIN, R.AP_CLERK, R.APPROVER, R.FINANCE_CONTROLLER, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(
        "pay.invoice.propose",
        "Propose coding, matching or a risk flag for an invoice",
        frozenset({R.AGENT}),
    ),
    PermissionSpec(
        "pay.invoice.approve",
        "Approve an invoice for payment",
        frozenset({R.APPROVER, R.FINANCE_CONTROLLER}),
    ),
)


@dataclass(frozen=True, slots=True)
class PayPlugin:
    """Minimal plugin satisfying ``pairly_core.plugins.WorkflowPlugin``."""

    id: str = "pay"
    name: str = "Pairly Pay"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)
    graphs: tuple[GraphSpec, ...] = ()


plugin = PayPlugin()

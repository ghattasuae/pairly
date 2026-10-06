# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Placeholder Pairly Hire plugin, registered in the ``pairly.workflows`` entry point group.

The full plugin (models, graphs, detectors, rules, router) is built in later work packages.
"""

from dataclasses import dataclass, field

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        "hire.candidate.read",
        "View candidates and their applications",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.INTERVIEWER, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(
        "hire.candidate.propose",
        "Propose a screening outcome for a candidate",
        frozenset({R.AGENT}),
    ),
    PermissionSpec(
        "hire.candidate.decide",
        "Advance or reject a candidate",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER}),
    ),
)


@dataclass(frozen=True, slots=True)
class HirePlugin:
    """Minimal plugin satisfying ``pairly_core.plugins.WorkflowPlugin``."""

    id: str = "hire"
    name: str = "Pairly Hire"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)


plugin = HirePlugin()

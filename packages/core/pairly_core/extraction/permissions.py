# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Extraction permissions and the per-workflow rule (same rule as documents).

    permission = "<workflow>.extraction.<action>" if the workflow declares it
                 else "core.extraction.<action>"

``correct`` is not an agent action, so the agent role can never correct an extraction:
corrections are always a person's. TODO(M2/M4): declare ``hire.extraction.*`` and
``pay.extraction.*`` in the plugins.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import Actor, BuiltinRole, PermissionSpec, RoleRegistry, authorize

R = BuiltinRole

EXTRACTION_READ: Final = "core.extraction.read"
EXTRACTION_CORRECT: Final = "core.extraction.correct"
ACTIONS = frozenset({"read", "correct"})

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        EXTRACTION_READ,
        "View extracted fields, their evidence and corrections",
        frozenset(
            {
                R.ADMIN,
                R.AUDITOR,
                R.READ_ONLY,
                R.RECRUITER,
                R.HIRING_MANAGER,
                R.INTERVIEWER,
                R.AP_CLERK,
                R.APPROVER,
                R.FINANCE_CONTROLLER,
                R.AGENT,
            }
        ),
    ),
    PermissionSpec(
        EXTRACTION_CORRECT,
        "Correct extracted fields",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.AP_CLERK}),
    ),
)


def extraction_permission(registry: RoleRegistry, workflow: str, action: str) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown extraction action {action!r}")
    specific = f"{workflow}.extraction.{action}"
    return specific if specific in registry else f"core.extraction.{action}"


def authorize_extraction(
    actor: Actor,
    registry: RoleRegistry,
    *,
    workflow: str,
    action: str,
    resource: object | None = None,
) -> None:
    """Raise ``PermissionDeniedError`` unless ``actor`` may ``action`` in ``workflow``."""
    authorize(actor, extraction_permission(registry, workflow, action), resource)

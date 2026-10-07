# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Document permissions and the per-workflow authorisation rule.

Core defines ``core.document.read`` and ``core.document.upload`` (registered through
``pairly_core.rbac.CORE_PERMISSION_MODULES``). Every document belongs to a workflow,
and a workflow may take over its documents' permissions by declaring
``<workflow>.document.read`` / ``<workflow>.document.upload`` in its plugin. The rule:

    permission = "<workflow>.document.<action>" if the workflow declares it
                 else "core.document.<action>"

So a workflow-scoped member (``workflow='pay'``), who holds no ``core.*`` permission,
can work with pay documents as soon as the pay plugin declares ``pay.document.*``; and
until a workflow declares them, only unscoped memberships reach its documents.
TODO(M2/M4): declare ``hire.document.*`` and ``pay.document.*`` in the plugins.

The agent role reads documents (``read`` is allowed for agents) but never uploads.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import Actor as _Actor
from pairly_core.rbac import (
    BuiltinRole,
    PermissionSpec,
    RoleRegistry,
    authorize,
)

R = BuiltinRole

DOCUMENT_READ: Final = "core.document.read"
DOCUMENT_UPLOAD: Final = "core.document.upload"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        DOCUMENT_READ,
        "View documents and download their content",
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
        DOCUMENT_UPLOAD,
        "Submit documents",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.AP_CLERK}),
    ),
)


def document_permission(registry: RoleRegistry, workflow: str, action: str) -> str:
    """The permission guarding ``action`` (``read`` or ``upload``) on ``workflow``'s documents."""
    if action not in {"read", "upload"}:
        raise ValueError(f"unknown document action {action!r}")
    specific = f"{workflow}.document.{action}"
    return specific if specific in registry else f"core.document.{action}"


def authorize_document(
    actor: _Actor,
    registry: RoleRegistry,
    *,
    workflow: str,
    action: str,
    resource: object | None = None,
) -> None:
    """Raise ``PermissionDeniedError`` unless ``actor`` may ``action`` in ``workflow``."""
    authorize(actor, document_permission(registry, workflow, action), resource)

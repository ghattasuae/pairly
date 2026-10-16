# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Proposal permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.proposal.propose``: suggest an action or leave a note for a person to decide.
  The agent holds it, and so does every member of staff, so a person may give it to an
  API key (a key's scopes must be held by its creator). It never decides anything.
* ``core.proposal.read``: see proposals. Staff, deciders, admins, auditors and the agent.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

PROPOSAL_PROPOSE: Final = "core.proposal.propose"
PROPOSAL_READ: Final = "core.proposal.read"

_STAFF = frozenset(
    {
        R.ADMIN,
        R.RECRUITER,
        R.HIRING_MANAGER,
        R.INTERVIEWER,
        R.AP_CLERK,
        R.APPROVER,
        R.FINANCE_CONTROLLER,
    }
)

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        PROPOSAL_PROPOSE,
        "Suggest an action or leave a note for a person to decide",
        _STAFF | {R.AGENT},
    ),
    PermissionSpec(PROPOSAL_READ, "See proposals", _STAFF | {R.AUDITOR, R.AGENT}),
)

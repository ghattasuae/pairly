# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Screening permissions (added to the Hire plugin's ``permissions``).

* ``hire.screening.read``: the explainability panel of an application (criterion
  scores, evidence, flags, versions). The hiring team, auditors and the agent.
* ``hire.screening.approve``: the shortlist decision on a screening (approve =
  shortlist, reject = not shortlisted). People who decide on candidates; a second
  factor, like every ``approve`` permission. Never the agent.
* ``hire.screening.override``: change a criterion score with a written reason (logged
  as a decision record, used for calibration). Recruiters and hiring managers, second
  factor (``override`` is not a step-up action by name, so the route asks for it).
* ``hire.screening.propose``: start a screening run. The agent only; people re-run a
  screening through the API, which starts the run as the agent after checking that the
  person may decide on candidates.
* ``hire.screening.manage``: the screening settings (blind mode, university names,
  gap months, injection classifier mode). Admins.

Interviewers read the panel but, while it is blind, see no name or contact details,
like everybody else.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

SCREENING_READ: Final = "hire.screening.read"
SCREENING_APPROVE: Final = "hire.screening.approve"
SCREENING_OVERRIDE: Final = "hire.screening.override"
SCREENING_PROPOSE: Final = "hire.screening.propose"
SCREENING_MANAGE: Final = "hire.screening.manage"

_HIRING = frozenset({R.RECRUITER, R.HIRING_MANAGER})

SCREENING_PERMISSIONS: Final[tuple[PermissionSpec, ...]] = (
    PermissionSpec(
        SCREENING_READ,
        "View screening results: scores, evidence, flags and versions",
        frozenset({R.ADMIN, *_HIRING, R.INTERVIEWER, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(
        SCREENING_APPROVE,
        "Decide a screening: shortlist the candidate or not",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        SCREENING_OVERRIDE,
        "Change a screening score with a written reason",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(SCREENING_PROPOSE, "Run a screening (the agent)", frozenset({R.AGENT})),
    PermissionSpec(
        SCREENING_MANAGE,
        "Change screening settings: blind mode, university names, checks",
        frozenset({R.ADMIN}),
    ),
)

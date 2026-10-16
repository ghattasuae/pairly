# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling permissions.

* ``hire.interview.read``: see interviews, panels and availability (the hiring team,
  auditors and the agent). An interviewer sees every interview they can read the
  candidate of; their own load and upcoming interviews come first.
* ``hire.interview.schedule``: book, move, cancel and send self-scheduling links, edit
  other people's interview availability, record no-shows (recruiters, hiring managers,
  admins). Not a second-factor action: booking a time is everyday work, and every change
  is audited and reversible by another change.

A person always edits their own availability and records the outcome of interviews
they sat on.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

INTERVIEW_READ: Final = "hire.interview.read"
INTERVIEW_SCHEDULE: Final = "hire.interview.schedule"

SCHEDULING_PERMISSIONS: Final[tuple[PermissionSpec, ...]] = (
    PermissionSpec(
        INTERVIEW_READ,
        "View interviews, panels and interview availability",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.INTERVIEWER, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(
        INTERVIEW_SCHEDULE,
        "Book, move and cancel interviews, and send self-scheduling links",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER}),
    ),
)

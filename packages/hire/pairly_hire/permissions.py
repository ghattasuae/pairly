# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hire permissions, all ``hire.<resource>.<action>``, with default role grants.

Who does what by default:

* Recruiters and hiring managers write jobs, ask the AI for drafts, edit rubrics and
  application forms, and lock rubrics. Recruiters (and admins) publish, pause and close
  jobs. Locking (``lock``) and publishing (``publish``) need a sign-in with a second
  factor, like every ``approve``, ``decide``, ``manage`` and ``export`` permission
  (``pairly_core.rbac.STEP_UP_ACTIONS``): a locked rubric is what candidates are later
  measured against, and a published job is visible to everyone.
* Interviewers read jobs, rubrics and the candidates they meet.
* Employees read the organisation's open jobs (referrals come later). They hold
  nothing else, and never see drafts (``SEES_DRAFTS``).
* Self-identification answers (``hire.selfid.read``) are for bias reporting only:
  admins and auditors, never the people who decide on candidates, and never the agent.
* The agent role only reads and proposes.
* Admins manage the Hire settings (careers page, retention, reply time).

Declaring ``hire.document.*`` makes the engine use them instead of ``core.document.*``
for documents of the ``hire`` workflow (CVs), so a member limited to Hire can open them.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

JOB_READ: Final = "hire.job.read"
JOB_WRITE: Final = "hire.job.write"
JOB_PUBLISH: Final = "hire.job.publish"
RUBRIC_READ: Final = "hire.rubric.read"
RUBRIC_WRITE: Final = "hire.rubric.write"
RUBRIC_LOCK: Final = "hire.rubric.lock"
CANDIDATE_READ: Final = "hire.candidate.read"
CANDIDATE_PROPOSE: Final = "hire.candidate.propose"
CANDIDATE_DECIDE: Final = "hire.candidate.decide"
SELFID_READ: Final = "hire.selfid.read"
SETTINGS_MANAGE: Final = "hire.settings.manage"
DOCUMENT_READ: Final = "hire.document.read"
DOCUMENT_UPLOAD: Final = "hire.document.upload"

# Who sees draft, paused and closed jobs; everyone else with ``hire.job.read`` sees
# open jobs only (an unannounced role stays confidential).
SEES_DRAFTS: Final = frozenset({JOB_WRITE, RUBRIC_READ})

_HIRING = frozenset({R.RECRUITER, R.HIRING_MANAGER})
_HIRING_TEAM = frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.INTERVIEWER})

PERMISSIONS: Final[tuple[PermissionSpec, ...]] = (
    PermissionSpec(
        JOB_READ,
        "View jobs (open jobs only, unless you work on hiring)",
        frozenset({*_HIRING_TEAM, R.AUDITOR, R.EMPLOYEE, R.AGENT}),
    ),
    PermissionSpec(
        JOB_WRITE,
        "Create and edit jobs and application forms, and ask for AI drafts",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(JOB_PUBLISH, "Publish, pause and close jobs", frozenset({R.ADMIN, R.RECRUITER})),
    PermissionSpec(
        RUBRIC_READ,
        "View rubrics and their versions",
        frozenset({*_HIRING_TEAM, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(RUBRIC_WRITE, "Edit rubric drafts and ask for AI drafts", _HIRING),
    PermissionSpec(RUBRIC_LOCK, "Lock a rubric so applications can be scored against it", _HIRING),
    PermissionSpec(
        CANDIDATE_READ,
        "View candidates and their applications",
        frozenset({*_HIRING_TEAM, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(
        CANDIDATE_PROPOSE, "Propose a screening outcome for a candidate", frozenset({R.AGENT})
    ),
    PermissionSpec(
        CANDIDATE_DECIDE,
        "Advance or reject a candidate",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        SELFID_READ,
        "View self-identification answers as totals, for bias reporting only",
        frozenset({R.ADMIN, R.AUDITOR}),
    ),
    PermissionSpec(
        SETTINGS_MANAGE,
        "Change Hire settings: careers page, retention, reply time",
        frozenset({R.ADMIN}),
    ),
    PermissionSpec(
        DOCUMENT_READ,
        "View CVs and other application files",
        frozenset({*_HIRING_TEAM, R.AUDITOR, R.AGENT}),
    ),
    PermissionSpec(DOCUMENT_UPLOAD, "Upload application files", frozenset({R.ADMIN, *_HIRING})),
)

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
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
* Employees read the organisation's open jobs and make referral links for them
  (``hire.referral.create``); they see only the stage of the people they referred
  (``hire.referral.read``), never scores or notes. They hold nothing else, and never
  see drafts (``SEES_DRAFTS``).
* Moving a candidate between pipeline stages is a decision about them
  (``hire.candidate.decide``, second factor). Notes and tags (``hire.note.write``) are
  for the whole hiring team. Customising a job's stages is ``hire.pipeline.write``.
* Messages to candidates: the hiring team drafts them, asks the AI for a personal
  paragraph, sends non-sensitive ones and submits sensitive ones (rejections, offers)
  for approval (``hire.message.create``). Approving a sensitive message is
  ``hire.message.approve`` (second factor); the person who submitted it can never
  approve it (segregation of duties in the approval engine).
* Self-identification answers (``hire.selfid.read``) are for bias reporting only:
  admins and auditors, never the people who decide on candidates, and never the agent.
* The agent role only reads and proposes.
* Admins manage the Hire settings (careers page, retention, reply time).
* Requisitions (M3): hiring managers, recruiters and admins request a role
  (``hire.requisition.create``). Approving one is ``hire.requisition.approve`` (second
  factor): recruiters (HR) and admins, and finance controllers for the finance step of a
  large budget. The person who requested a role can never approve it (segregation of
  duties in the approval engine). Finance controllers and auditors read requisitions.
* Interview plans (stages, panels, scorecard templates) are written by the people who
  write jobs (``hire.interview_plan.write``). Every member of the hiring team submits
  their own scorecard for the interviews they sit on; reading everybody's scorecards and
  the debrief summary is ``hire.scorecard.read`` (never for a panelist who has not
  submitted their own yet, enforced by the API).

* Reference checks (M3) are for the people who decide on candidates: recruiters, hiring
  managers and admins read them (``hire.reference.read``) and ask referees
  (``hire.reference.write``). Interviewers and auditors never read referees' answers.

* Offers (M3): the hiring team builds and sends them (``hire.offer.create``); the
  approval chain is the hiring manager, then HR, and finance above a threshold
  (``hire.offer.approve``, second factor; the submitter never approves). Compensation
  is visible only with ``hire.offer.compensation`` (the hiring team, admins and finance
  controllers); auditors read offers without it.

* The pre-employment checklist (M3) holds identity documents: only HR (recruiters) and
  admins configure it, request documents, open files, verify or waive them
  (``hire.checklist.read``, ``hire.checklist.write``). Hiring managers and interviewers
  never see the files.

* Candidate experience survey results (M3) are totals for HR and admins
  (``hire.survey.read``); a single answer is visible only when its author chose to
  attach their name.

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
NOTE_WRITE: Final = "hire.note.write"
PIPELINE_WRITE: Final = "hire.pipeline.write"
MESSAGE_CREATE: Final = "hire.message.create"
MESSAGE_APPROVE: Final = "hire.message.approve"
REFERRAL_CREATE: Final = "hire.referral.create"
REFERRAL_READ: Final = "hire.referral.read"
REQUISITION_READ: Final = "hire.requisition.read"
REQUISITION_CREATE: Final = "hire.requisition.create"
REQUISITION_APPROVE: Final = "hire.requisition.approve"
PLAN_WRITE: Final = "hire.interview_plan.write"
SCORECARD_READ: Final = "hire.scorecard.read"
REFERENCE_READ: Final = "hire.reference.read"
REFERENCE_WRITE: Final = "hire.reference.write"
OFFER_READ: Final = "hire.offer.read"
OFFER_WRITE: Final = "hire.offer.create"
OFFER_APPROVE: Final = "hire.offer.approve"
OFFER_COMPENSATION: Final = "hire.offer.compensation"
CHECKLIST_READ: Final = "hire.checklist.read"
CHECKLIST_WRITE: Final = "hire.checklist.write"
SURVEY_READ: Final = "hire.survey.read"
ANALYTICS_READ: Final = "hire.analytics.read"
GOVERNANCE_READ: Final = "hire.governance.read"
BIAS_READ: Final = "hire.bias.read"
BIAS_EXPORT: Final = "hire.bias.export"
FAIRNESS_RUN: Final = "hire.fairness.run"
CALIBRATION_WRITE: Final = "hire.calibration.write"

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
    PermissionSpec(
        NOTE_WRITE,
        "Add notes and tags to candidates",
        frozenset({R.ADMIN, *_HIRING, R.INTERVIEWER}),
    ),
    PermissionSpec(
        PIPELINE_WRITE, "Customise a job's pipeline stages", frozenset({R.ADMIN, R.RECRUITER})
    ),
    PermissionSpec(
        MESSAGE_CREATE,
        "Draft messages to candidates, send updates, and submit rejections and offers for approval",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        MESSAGE_APPROVE,
        "Approve a rejection or an offer before it is sent",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        REFERRAL_CREATE,
        "Make referral links for open jobs",
        frozenset({*_HIRING_TEAM, R.EMPLOYEE}),
    ),
    PermissionSpec(
        REFERRAL_READ,
        "See the stage of the people you referred",
        frozenset({*_HIRING_TEAM, R.EMPLOYEE}),
    ),
    PermissionSpec(
        REQUISITION_READ,
        "View requisitions (role requests) and their approvals",
        frozenset({R.ADMIN, *_HIRING, R.FINANCE_CONTROLLER, R.AUDITOR}),
    ),
    PermissionSpec(
        REQUISITION_CREATE,
        "Request a new role (a requisition) for approval",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        REQUISITION_APPROVE,
        "Approve a requisition before a job can be opened from it",
        frozenset({R.ADMIN, R.RECRUITER, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        PLAN_WRITE,
        "Edit a job's interview plan: stages, panels and scorecards",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        SCORECARD_READ,
        "View everyone's interview scorecards and the debrief summary",
        frozenset({R.ADMIN, *_HIRING, R.AUDITOR}),
    ),
    PermissionSpec(
        REFERENCE_READ,
        "View reference checks: referees, their answers and the summary",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        OFFER_READ,
        "View offers and their approval status (compensation needs its own permission)",
        frozenset({R.ADMIN, *_HIRING, R.FINANCE_CONTROLLER, R.AUDITOR}),
    ),
    PermissionSpec(
        OFFER_WRITE,
        "Build offers from templates, submit them for approval, send and withdraw them",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        OFFER_APPROVE,
        "Approve an offer before it is sent (hiring manager, HR, finance above a threshold)",
        frozenset({R.ADMIN, *_HIRING, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        OFFER_COMPENSATION,
        "See and set the compensation in offers",
        frozenset({R.ADMIN, *_HIRING, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        CHECKLIST_READ,
        "View pre-employment checklists and open their documents (ID, degree, visa)",
        frozenset({R.ADMIN, R.RECRUITER}),
    ),
    PermissionSpec(
        CHECKLIST_WRITE,
        "Set the documents to collect per job, request them, verify or waive them",
        frozenset({R.ADMIN, R.RECRUITER}),
    ),
    PermissionSpec(
        SURVEY_READ,
        "View candidate experience survey results (totals; named answers only by choice)",
        frozenset({R.ADMIN, R.RECRUITER}),
    ),
    PermissionSpec(
        ANALYTICS_READ,
        "View hiring analytics, fairness test results and calibration agreement",
        frozenset({R.ADMIN, R.RECRUITER}),
    ),
    PermissionSpec(
        GOVERNANCE_READ,
        "View AI governance counts: guardrail findings on CVs and calibration agreement"
        " (numbers only, no candidate data)",
        frozenset({R.ADMIN, R.RECRUITER, R.AUDITOR}),
    ),
    PermissionSpec(
        BIAS_READ,
        "View the bias and adverse-impact report (group totals only, small groups hidden)",
        frozenset({R.ADMIN, R.RECRUITER, R.AUDITOR}),
    ),
    PermissionSpec(
        BIAS_EXPORT,
        "Export the bias audit pack (needs two-factor sign-in, audited)",
        frozenset({R.ADMIN, R.AUDITOR}),
    ),
    PermissionSpec(
        FAIRNESS_RUN,
        "Start a counterfactual fairness test on screened applications",
        frozenset({R.ADMIN, R.RECRUITER}),
    ),
    PermissionSpec(
        CALIBRATION_WRITE,
        "Label applications per criterion to calibrate the screening rubric",
        frozenset({R.ADMIN, *_HIRING}),
    ),
    PermissionSpec(
        REFERENCE_WRITE,
        "Ask referees for a reference, send their link again or cancel it",
        frozenset({R.ADMIN, *_HIRING}),
    ),
)

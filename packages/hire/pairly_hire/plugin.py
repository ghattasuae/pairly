# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pairly Hire plugin, registered in the ``pairly.workflows`` entry point group.

Declared on one object (``pairly_core.plugins.WorkflowPlugin`` plus optional attributes):

* ``permissions``  ``hire.*`` with default role grants (``permissions`` and
                   ``screening.permissions``).
* ``graphs``       ``hire_screening`` (``screening.spec``): CV to a person's shortlist
                   decision, with its ``hire.screening`` approval type and the
                   ``shortlist`` final action.
                   ``hire_message``: a sensitive message (rejection, offer) waits for a
                   person's approval and is sent by the ``send_candidate_message`` final
                   action (``pairly_hire.messaging.graph``).
                   ``hire_requisition``: a requested role waits for its approval chain
                   (``requisitions/approval_rules.yaml``), then the
                   ``approve_requisition`` final action marks it approved.
                   ``hire_offer``: an offer waits for its approval chain
                   (``offers/approval_rules.yaml``), then ``approve_offer`` marks it
                   approved; sending it is a sensitive message (``hire_message``).
* ``api_router``   the ``/v1/hire`` routes, imported only when called.
* ``demo_seeds``   ``hire-demo`` for ``pairly-admin seed``.
* ``migrations_dir``  the ``hire`` Alembic branch (``migrations/``).
* ``prompt_roots``  the versioned prompts ``hire_jd_draft``, ``hire_rubric_draft`` and
                   ``hire_cv_score`` and ``hire_message_draft``.
* ``retention_rules``  ``hire.applications.rejected`` (dry run, per-organisation months)
                   and ``hire.checklist.documents`` (checklist files after hire).
* ``search_sources``  ``hire_candidate`` (talent rediscovery, consent and retention).
* ``scheduled_jobs``  ``hire_screening_start``: starts the screening of submitted
                   applications whose CV is ready, every minute (``screening.start``);
                   ``hire_sla_drafts`` (anti-ghosting), ``hire_rediscovery_index`` and
                   ``hire_interview_reminders`` (24 hours and 1 hour before interviews);
                   ``hire_scorecard_reminders``: feedback reminders 2 and 24 hours after
                   an interview, and "remind me later" (``interviewing.reminders``);
                   ``hire_checklist_retention``: checklist file retention after hire;
                   ``hire_survey_invites``: the candidate experience survey;
                   ``hire_erasure_objects``: removes the stored files of erasures a
                   person started, then completes their requests (``erasure``).
* ``feature_flags``  ``hire.scheduling.google`` (off): Google Calendar and Meet for
                   interviews, once the deployment has a Google OAuth client.

Importing this module registers the Hire tables on ``Base.metadata``, wires screening
into the pipeline hooks (blind mode per application, screening results, the blind
redactor for free text; ``screening.reviewer.register``) and registers the approver's
view of a message while blind (``messaging.approval_view.register``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pairly_core.flags.registry import FlagSpec
from pairly_core.jobs.registry import JobSpec
from pairly_core.plugins import DemoSeed
from pairly_core.rbac import PermissionSpec
from pairly_core.retention.rules import RetentionRule
from pairly_core.search.sources import SearchSource
from pairly_core.workflow.spec import ApprovalType, GraphSpec
from pairly_hire import models  # noqa: F401  (registers the Hire tables)
from pairly_hire.erasure import JOB_NAME as ERASURE_JOB
from pairly_hire.erasure import run_erasure_job
from pairly_hire.interviewing import models as interviewing_models  # noqa: F401  (tables)
from pairly_hire.interviewing.reminders import JOB_NAME as SCORECARD_REMINDERS_JOB
from pairly_hire.interviewing.reminders import run_scorecard_reminders_job
from pairly_hire.messaging import approval_view as message_approval_view
from pairly_hire.messaging import graph as message_graph
from pairly_hire.messaging import models as messaging_models  # noqa: F401  (tables)
from pairly_hire.messaging.sla import JOB_NAME as SLA_JOB
from pairly_hire.messaging.sla import run_sla_job
from pairly_hire.offers import graph as offer_graph
from pairly_hire.offers import models as offer_models  # noqa: F401  (tables)
from pairly_hire.onboarding import models as onboarding_models  # noqa: F401  (tables)
from pairly_hire.onboarding.service import JOB_NAME as CHECKLIST_RETENTION_JOB
from pairly_hire.onboarding.service import run_retention_job as run_checklist_retention_job
from pairly_hire.permissions import (
    CANDIDATE_READ,
    MESSAGE_APPROVE,
    MESSAGE_CREATE,
    OFFER_APPROVE,
    OFFER_READ,
    OFFER_WRITE,
    PERMISSIONS,
    REQUISITION_APPROVE,
    REQUISITION_CREATE,
    REQUISITION_READ,
)
from pairly_hire.pipeline import models as pipeline_models  # noqa: F401  (tables)
from pairly_hire.pipeline.rediscovery import JOB_NAME as REDISCOVERY_JOB
from pairly_hire.pipeline.rediscovery import SEARCH_SOURCES, run_index_job
from pairly_hire.portal import models as portal_models  # noqa: F401  (tables)
from pairly_hire.references import models as reference_models  # noqa: F401  (tables)
from pairly_hire.requisitions import graph as requisition_graph
from pairly_hire.requisitions import models as requisition_models  # noqa: F401  (tables)
from pairly_hire.retention import RETENTION_RULES
from pairly_hire.scheduling import models as scheduling_models  # noqa: F401  (tables)
from pairly_hire.scheduling.permissions import SCHEDULING_PERMISSIONS
from pairly_hire.scheduling.providers import FEATURE_FLAGS
from pairly_hire.scheduling.reminders import JOB_NAME as REMINDERS_JOB
from pairly_hire.scheduling.reminders import run_reminders_job
from pairly_hire.screening import models as screening_models  # noqa: F401  (its tables)
from pairly_hire.screening import reviewer as screening_reviewer
from pairly_hire.screening.permissions import SCREENING_PERMISSIONS
from pairly_hire.screening.spec import HIRE_SCREENING
from pairly_hire.screening.start import SCHEDULED_JOBS as SCREENING_JOBS
from pairly_hire.seed import seed_hire_demo
from pairly_hire.selfid import models as selfid_models  # noqa: F401  (registers its table)
from pairly_hire.survey import models as survey_models  # noqa: F401  (tables)
from pairly_hire.survey.service import JOB_NAME as SURVEY_JOB
from pairly_hire.survey.service import run_invites_job as run_survey_invites_job

if TYPE_CHECKING:
    from fastapi import APIRouter

_HERE = Path(__file__).resolve().parent

# The pipeline and messaging read blind mode and screening results from screening.
screening_reviewer.register()
# Approvers of a message see the candidate's name, email and phone masked while blind.
message_approval_view.register()

HIRE_MESSAGE: GraphSpec = GraphSpec(
    workflow="hire",
    name=message_graph.GRAPH,
    version=message_graph.VERSION,
    build=message_graph.build_graph,
    start_permission=MESSAGE_CREATE,
    read_permission=CANDIDATE_READ,
    approval_types=(
        ApprovalType(
            message_graph.APPROVAL_TYPE,
            MESSAGE_APPROVE,
            message_graph.APPROVAL_RULES_V1,
            "Message to a candidate",
        ),
    ),
    final_actions={message_graph.FINAL_ACTION: message_graph.send_candidate_message},
    description="A rejection or an offer waits for a person's approval, then is sent.",
)

HIRE_REQUISITION: GraphSpec = GraphSpec(
    workflow="hire",
    name=requisition_graph.GRAPH,
    version=requisition_graph.VERSION,
    build=requisition_graph.build_graph,
    start_permission=REQUISITION_CREATE,
    read_permission=REQUISITION_READ,
    approval_types=(
        ApprovalType(
            requisition_graph.APPROVAL_TYPE,
            REQUISITION_APPROVE,
            requisition_graph.APPROVAL_RULES_V1,
            "Requisition (a new role)",
        ),
    ),
    final_actions={requisition_graph.FINAL_ACTION: requisition_graph.approve_requisition},
    description="A requested role waits for HR (and finance above a budget), then opens.",
)

HIRE_OFFER: GraphSpec = GraphSpec(
    workflow="hire",
    name=offer_graph.GRAPH,
    version=offer_graph.VERSION,
    build=offer_graph.build_graph,
    start_permission=OFFER_WRITE,
    read_permission=OFFER_READ,
    approval_types=(
        ApprovalType(
            offer_graph.APPROVAL_TYPE,
            OFFER_APPROVE,
            offer_graph.APPROVAL_RULES_V1,
            "Offer to a candidate",
        ),
    ),
    final_actions={offer_graph.FINAL_ACTION: offer_graph.approve_offer},
    description="An offer waits for the hiring manager, HR and finance above a threshold.",
)

SCHEDULED_JOBS: tuple[JobSpec, ...] = (
    *SCREENING_JOBS,
    JobSpec(
        SLA_JOB,
        "Draft a status update for candidates waiting too long, and alert the recruiter",
        "every 15 minutes",
        run=run_sla_job,
        every_minutes=15,
    ),
    JobSpec(
        REDISCOVERY_JOB,
        "Index past applicants who agreed to be considered for future roles",
        "every 15 minutes",
        run=run_index_job,
        every_minutes=15,
    ),
    JobSpec(
        REMINDERS_JOB,
        "Remind candidates and interviewers 24 hours and 1 hour before each interview",
        "every 5 minutes",
        run=run_reminders_job,
        every_minutes=5,
    ),
    JobSpec(
        SCORECARD_REMINDERS_JOB,
        "Remind interviewers 2 and 24 hours after an interview to submit their scorecard "
        "(SLA: the interview plan's hours after the interview, 24 by default)",
        "every 5 minutes",
        run=run_scorecard_reminders_job,
        every_minutes=5,
    ),
    JobSpec(
        CHECKLIST_RETENTION_JOB,
        "Set the retention of pre-employment checklist files once the candidate is hired, "
        "and remove the files past it",
        "every hour",
        run=run_checklist_retention_job,
        every_minutes=60,
    ),
    JobSpec(
        SURVEY_JOB,
        "Invite candidates to the experience survey once their process ended "
        "(hired, rejected once told, withdrawn)",
        "every 15 minutes",
        run=run_survey_invites_job,
        every_minutes=15,
    ),
    JobSpec(
        ERASURE_JOB,
        "Remove the stored files of candidate erasures a person started, then complete them",
        "every 5 minutes",
        run=run_erasure_job,
        every_minutes=5,
    ),
)


def api_router() -> APIRouter:
    from pairly_hire.api import router

    return router


@dataclass(frozen=True, slots=True)
class HirePlugin:
    id: str = "hire"
    name: str = "Pairly Hire"
    permissions: tuple[PermissionSpec, ...] = field(
        default=(*PERMISSIONS, *SCREENING_PERMISSIONS, *SCHEDULING_PERMISSIONS)
    )
    graphs: tuple[GraphSpec, ...] = (HIRE_SCREENING, HIRE_MESSAGE, HIRE_REQUISITION, HIRE_OFFER)
    demo_seeds: Mapping[str, DemoSeed] = field(
        default_factory=lambda: {"hire-demo": seed_hire_demo}
    )
    migrations_dir: Path = _HERE / "migrations"
    prompt_roots: tuple[Path, ...] = (_HERE / "prompts",)
    retention_rules: tuple[RetentionRule, ...] = RETENTION_RULES
    search_sources: tuple[SearchSource, ...] = SEARCH_SOURCES
    scheduled_jobs: tuple[JobSpec, ...] = SCHEDULED_JOBS
    feature_flags: tuple[FlagSpec, ...] = FEATURE_FLAGS

    def api_router(self) -> APIRouter:
        return api_router()


plugin = HirePlugin()

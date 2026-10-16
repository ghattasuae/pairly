# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Declared scheduled jobs.

The schedule is a human description for operators (the platform operations view, brief
5.2 item 15); the worker owns the exact cron timing. A test keeps the two in step: every
cron job the worker schedules is declared here, and every declared core job is scheduled.

A workflow plugin declares its own jobs in ``scheduled_jobs`` (named ``<plugin id>_...``).
A plugin job that sets ``run`` and ``every_minutes`` is scheduled by the worker: every
``every_minutes`` minutes (a divisor of 60), one run at a time, recorded in ``job_runs``
like core jobs. ``run`` gets a ``JobContext`` (sessions, the workflow runtime and the
role registry) and returns a short summary.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pairly_core.rbac import RoleRegistry
    from pairly_core.workflow.runtime import WorkflowRuntime

JOB_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SWEEP_DOCUMENTS: Final = "sweep_documents"
NIGHTLY_AUDIT_VERIFY: Final = "nightly_audit_verify"
APPROVAL_SLA_ESCALATION: Final = "approval_sla_escalation"
WORKFLOW_RESUME_RECONCILE: Final = "workflow_resume_reconcile"
DELIVER_WEBHOOKS: Final = "deliver_webhooks"
RETENTION_DRY_RUN: Final = "retention_dry_run"
NOTIFICATION_DELIVERY: Final = "notification_delivery"
NOTIFICATION_DIGEST: Final = "notification_digest"
AUDIT_ANCHOR_NIGHTLY: Final = "audit_anchor_nightly"
APPEAL_SLA_ESCALATION: Final = "appeal_sla_escalation"
AUDIT_DENIAL_FLUSH: Final = "audit_denial_flush"
DECISION_JOBS_SWEEP: Final = "decision_jobs_sweep"
BATCH_URL_SWEEP: Final = "batch_url_sweep"


class JobRegistryError(ValueError):
    """An invalid, duplicate or unknown job (a programming error)."""


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a plugin job gets from the worker."""

    sessionmaker: async_sessionmaker[AsyncSession]
    runtime: WorkflowRuntime
    registry: RoleRegistry


PluginJob = Callable[[JobContext], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    description: str
    schedule: str
    # Plugin jobs only: the coroutine the worker runs, and how often.
    run: PluginJob | None = None
    every_minutes: int | None = None

    def __post_init__(self) -> None:
        if not JOB_NAME_RE.fullmatch(self.name):
            raise JobRegistryError(f"invalid job name {self.name!r}")
        if not self.description or not self.schedule:
            raise JobRegistryError(f"job {self.name!r} needs a description and a schedule")
        if (self.run is None) != (self.every_minutes is None):
            raise JobRegistryError(f"job {self.name!r}: set both run and every_minutes, or none")
        if self.every_minutes is not None and (
            not 1 <= self.every_minutes <= 60 or 60 % self.every_minutes
        ):
            raise JobRegistryError(f"job {self.name!r}: every_minutes must divide 60")

    @property
    def minutes(self) -> set[int]:
        """The minutes of each hour the worker runs it (plugin jobs)."""
        if self.every_minutes is None:
            return set()
        return set(range(0, 60, self.every_minutes))


CORE_JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        SWEEP_DOCUMENTS,
        "Re-queue documents whose scan or parse stalled",
        "every minute, and at worker start",
    ),
    JobSpec(
        NIGHTLY_AUDIT_VERIFY,
        "Verify every organisation's audit chain",
        "daily at PAIRLY_AUDIT_VERIFY_HOUR:PAIRLY_AUDIT_VERIFY_MINUTE UTC",
    ),
    JobSpec(
        APPROVAL_SLA_ESCALATION,
        "Escalate approvals past their SLA",
        "every 5 minutes",
    ),
    JobSpec(
        WORKFLOW_RESUME_RECONCILE,
        "Resume workflow runs whose approvals were decided",
        "every minute, and at worker start",
    ),
    JobSpec(
        DELIVER_WEBHOOKS,
        "Fan out the webhook outbox and send due deliveries",
        "every minute, and at worker start",
    ),
    JobSpec(
        RETENTION_DRY_RUN,
        "Count what each retention rule would delete (never deletes)",
        "daily at 02:15 UTC",
    ),
    JobSpec(
        NOTIFICATION_DELIVERY,
        "Send due email and Slack notifications, with retries",
        "every minute, and at worker start",
    ),
    JobSpec(
        NOTIFICATION_DIGEST,
        "Send each person one digest email of their held notifications",
        "daily at PAIRLY_DIGEST_HOUR_UTC:00 UTC",
    ),
    JobSpec(
        AUDIT_ANCHOR_NIGHTLY,
        "Sign each organisation's audit chain head and write it to the anchor store",
        "daily at PAIRLY_AUDIT_ANCHOR_HOUR:PAIRLY_AUDIT_ANCHOR_MINUTE UTC",
    ),
    JobSpec(
        APPEAL_SLA_ESCALATION,
        "Escalate appeals past their SLA",
        "every 5 minutes",
    ),
    JobSpec(
        AUDIT_DENIAL_FLUSH,
        "Write the summary events of rate-limited authorisation denials",
        "every 5 minutes",
    ),
    JobSpec(
        DECISION_JOBS_SWEEP,
        "Run compliance exports and chain verifications whose job was lost",
        "every minute, and at worker start",
    ),
    JobSpec(
        BATCH_URL_SWEEP,
        "Fail batch URL items left pending 15 minutes after their batch (timed_out)",
        "every 5 minutes",
    ),
)


class JobRegistry:
    def __init__(self, jobs: Iterable[JobSpec] = CORE_JOBS) -> None:
        self._jobs: dict[str, JobSpec] = {}
        for job in jobs:
            self.register(job)

    def register(self, job: JobSpec) -> None:
        if job.name in self._jobs:
            raise JobRegistryError(f"duplicate job {job.name!r}")
        self._jobs[job.name] = job

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    @property
    def jobs(self) -> list[JobSpec]:
        return sorted(self._jobs.values(), key=lambda j: j.name)

    def require(self, name: str) -> JobSpec:
        try:
            return self._jobs[name]
        except KeyError:
            raise JobRegistryError(f"unknown job {name!r}") from None


def build_job_registry(plugins: Iterable[Any] = ()) -> JobRegistry:
    """Core jobs plus every plugin's optional ``scheduled_jobs`` (named ``<id>_...``)."""
    registry = JobRegistry()
    for plugin in plugins:
        for job in getattr(plugin, "scheduled_jobs", ()) or ():
            if not isinstance(job, JobSpec):
                raise JobRegistryError(f"plugin {plugin.id!r}: scheduled_jobs must be JobSpec")
            if not job.name.startswith(f"{plugin.id}_"):
                raise JobRegistryError(
                    f"plugin {plugin.id!r}: job {job.name!r} is outside its namespace"
                )
            registry.register(job)
    return registry

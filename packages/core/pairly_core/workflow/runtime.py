# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Workflow runtime: start, resume and cancel LangGraph runs, durably and per tenant.

Lifecycle of a run (``workflow_runs.status``)::

    running --(interrupt for approval)--> waiting_approval --(decided, resumed)--> running
    running --> completed | failed            any non-terminal --(cancel)--> cancelled

* ``start_run``: the actor must hold the graph's start permission (a ``propose``,
  ``create`` or ``submit`` action, so agents can start proposals). Inserts the run (thread id
  ``"<org_id>:<run_id>"``), audits ``core.workflow.run_started`` and invokes the graph.
* ``resume_run``: the ONLY way to resume a run. It locks the run, checks that it waits
  for an approval that belongs to it and that a human decision on that approval is
  recorded (status approved, rejected or changes requested), claims the run, and then
  resumes the graph. The resume value only names the approval; the waiting node reads
  the decision from the database and the executor re-checks it again before any final
  action. A refused resume is audited (``core.workflow.resume_refused``).
* Retries and timeouts per node come from LangGraph (``retry_policy`` and ``timeout`` on
  ``add_node``); ``node_retry`` gives a policy that never retries Pairly refusals.
* A node that raises after its retries fails the run (``failed`` with a short error, no
  personal data). Fail closed: nothing is approved or executed on a failure.

Every status change writes an audit event with ``entity_type`` ``workflow_run`` and the
run id, so a run's whole history is one audit query.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, RetryPolicy
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks
from pairly_core.approvals.models import DECIDED_STATUSES, Approval
from pairly_core.approvals.service import cancel_approval, canonical_payload, db_now
from pairly_core.audit import ActorType, actor_ref, append_audit
from pairly_core.db import TenantError, set_tenant
from pairly_core.errors import ConflictError, ForbiddenError, NotFoundError, PairlyError
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import Actor, ActorKind, RoleRegistry, authorize
from pairly_core.workflow.checkpoint import TenantCheckpointer, org_of_thread
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.models import RunStatus, WorkflowRun, thread_id_for
from pairly_core.workflow.nodes import INTERRUPT_KIND
from pairly_core.workflow.services import NO_SERVICES, GraphServices
from pairly_core.workflow.spec import ApprovalType, GraphSpec, SpecError

log = structlog.get_logger(__name__)

RUNTIME_ACTOR_ID = "pairly.workflow.runtime"
# Bound on immediate re-resumes when a decision lands while the run was still saving
# its interrupt; the reconciliation job covers anything beyond it.
_MAX_INLINE_RESUMES = 3


class RunActions:
    STARTED = "core.workflow.run_started"
    WAITING = "core.workflow.run_waiting"
    RESUMED = "core.workflow.run_resumed"
    RESUME_REFUSED = "core.workflow.resume_refused"
    COMPLETED = "core.workflow.run_completed"
    FAILED = "core.workflow.run_failed"
    CANCELLED = "core.workflow.run_cancelled"


class RunNotFoundError(NotFoundError):
    code = "run_not_found"
    title = "Workflow run not found"


class GraphNotFoundError(NotFoundError):
    code = "graph_not_found"
    title = "Workflow graph not found"


class ResumeRefusedError(ConflictError):
    code = "resume_refused"
    title = "The run cannot be resumed"

    def __init__(self, reason: str) -> None:
        super().__init__(f"resume refused: {reason}")
        self.reason = reason


def _never_retry_refusals(exc: Exception) -> bool:
    if isinstance(exc, PairlyError | TenantError | ValueError | TypeError | LookupError):
        return False
    return not isinstance(exc, RuntimeError)


def node_retry(max_attempts: int = 3, initial_interval: float = 0.5) -> RetryPolicy:
    """A node retry policy that retries transient errors but never a refusal."""
    return RetryPolicy(
        max_attempts=max_attempts,
        initial_interval=initial_interval,
        retry_on=_never_retry_refusals,
    )


@dataclass(frozen=True, slots=True)
class RunView:
    id: uuid.UUID
    org_id: uuid.UUID
    workflow: str
    graph_name: str
    graph_version: int
    thread_id: str
    status: str
    entity_type: str | None
    entity_id: str | None
    started_by_type: str
    started_by_id: str
    pending_approval_id: uuid.UUID | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, run: WorkflowRun) -> RunView:
        return cls(
            id=run.id,
            org_id=run.org_id,
            workflow=run.workflow,
            graph_name=run.graph_name,
            graph_version=run.graph_version,
            thread_id=run.thread_id,
            status=run.status,
            entity_type=run.entity_type,
            entity_id=run.entity_id,
            started_by_type=run.started_by_type,
            started_by_id=run.started_by_id,
            pending_approval_id=run.pending_approval_id,
            error=run.error,
            created_at=run.created_at,
            updated_at=run.updated_at,
            completed_at=run.completed_at,
        )


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, ExecutorRefusedError):
        return f"executor_refused: {exc.reason}"[:500]
    if isinstance(exc, PairlyError):
        return f"{exc.code}: {exc.detail}"[:500]
    # Never the message: it may hold personal data or SQL.
    return type(exc).__name__[:500]


def _approval_from_interrupts(interrupts: Iterable[Any]) -> uuid.UUID | None:
    for item in interrupts:
        value = getattr(item, "value", None)
        if isinstance(value, Mapping) and value.get("kind") == INTERRUPT_KIND:
            try:
                return uuid.UUID(str(value.get("approval_id")))
            except ValueError:
                return None
    return None


class WorkflowRuntime:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        checkpointer: TenantCheckpointer,
        registry: RoleRegistry,
        graphs: Iterable[GraphSpec] = (),
        hooks: ApprovalHooks = NO_HOOKS,
        services: GraphServices = NO_SERVICES,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.checkpointer = checkpointer
        self.registry = registry
        self.hooks = hooks
        self.services = services
        self._graphs: dict[tuple[str, str], GraphSpec] = {}
        self._approval_types: dict[str, ApprovalType] = {}
        for spec in graphs:
            self._register(spec)

    @classmethod
    def from_plugins(cls, plugins: Iterable[WorkflowPlugin], **kwargs: Any) -> WorkflowRuntime:
        graphs = [g for plugin in plugins for g in plugin.graphs]
        return cls(graphs=graphs, **kwargs)

    def _register(self, spec: GraphSpec) -> None:
        if spec.key in self._graphs:
            raise SpecError(f"duplicate graph {spec.workflow}.{spec.name}")
        for perm in (spec.start_permission, spec.read_permission):
            if perm not in self.registry:
                raise SpecError(f"graph {spec.name!r} uses unknown permission {perm!r}")
        for kind in spec.approval_types:
            if kind.permission not in self.registry:
                raise SpecError(f"approval type {kind.key!r} uses unknown permission")
            known = self._approval_types.get(kind.key)
            if known is not None and known != kind:
                raise SpecError(f"approval type {kind.key!r} declared twice differently")
            self._approval_types[kind.key] = kind
        self._graphs[spec.key] = spec

    @property
    def graphs(self) -> list[GraphSpec]:
        return list(self._graphs.values())

    def spec(self, workflow: str, name: str) -> GraphSpec:
        spec = self._graphs.get((workflow, name))
        if spec is None:
            raise GraphNotFoundError()
        return spec

    def approval_type(self, key: str) -> ApprovalType | None:
        return self._approval_types.get(key)

    async def check(self) -> None:
        """Startup check: the checkpoint schema matches the installed checkpointer."""
        await self.checkpointer.check_schema()

    # ---------------------------------------------------------------- sessions

    def _session(self, org_id: uuid.UUID) -> AsyncSession:
        session = self.sessionmaker()
        set_tenant(session, org_id, None)
        return session

    def _context(self, run: WorkflowRun, spec: GraphSpec) -> WorkflowContext:
        return WorkflowContext(
            org_id=run.org_id,
            run_id=run.id,
            graph=spec,
            started_by_type=ActorType(run.started_by_type),
            started_by_id=run.started_by_id,
            sessionmaker=self.sessionmaker,
            registry=self.registry,
            hooks=self.hooks,
            services=self.services,
        )

    async def _audit(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        action: str,
        payload: Mapping[str, Any] | None = None,
        actor: Actor | None = None,
    ) -> None:
        if actor is not None:
            kind, actor_id = actor_ref(actor)
        else:
            kind, actor_id = ActorType.SYSTEM, RUNTIME_ACTOR_ID
        await append_audit(
            session,
            action=action,
            actor_type=kind,
            actor_id=actor_id,
            entity_type="workflow_run",
            entity_id=run.id,
            workflow=run.workflow,
            payload={
                "graph": run.graph_name,
                "graph_version": run.graph_version,
                **(payload or {}),
            },
        )

    # ---------------------------------------------------------------- public API

    async def start_run(
        self,
        actor: Actor,
        *,
        workflow: str,
        graph: str,
        input: Mapping[str, Any],
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> RunView:
        spec = self.spec(workflow, graph)
        authorize(actor, spec.start_permission)
        state, _ = canonical_payload(input)
        run_id = uuid.uuid4()
        kind, actor_id = actor_ref(actor)
        async with self._session(actor.org_id) as session, session.begin():
            now = await db_now(session)
            run = WorkflowRun(
                id=run_id,
                org_id=actor.org_id,
                workflow=spec.workflow,
                graph_name=spec.name,
                graph_version=spec.version,
                thread_id=thread_id_for(actor.org_id, run_id),
                status=RunStatus.RUNNING,
                entity_type=entity_type,
                entity_id=entity_id,
                started_by_type=kind.value,
                started_by_id=actor_id,
                created_at=now,
                updated_at=now,
                claimed_at=now,
            )
            session.add(run)
            await session.flush()
            await self._audit(session, run, RunActions.STARTED, actor=actor)
        return await self._drive(run, spec, state)

    async def resume_run(self, org_id: uuid.UUID, run_id: uuid.UUID) -> RunView:
        """Resume a run whose approval has a recorded human decision. Else refuse."""
        claimed = await self._claim(org_id, run_id)
        if isinstance(claimed, str):
            await self._refused(org_id, run_id, claimed)
            raise ResumeRefusedError(claimed)
        run, approval_id = claimed
        return await self._drive(
            run, self.spec(run.workflow, run.graph_name), self._resume_value(approval_id)
        )

    async def cancel_run(self, actor: Actor, run_id: uuid.UUID) -> RunView:
        if actor.kind is not ActorKind.USER:
            raise ForbiddenError("only people can cancel runs")
        async with self._session(actor.org_id) as session, session.begin():
            run = await session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
            )
            if run is None:
                raise RunNotFoundError()
            authorize(actor, self.spec(run.workflow, run.graph_name).start_permission, run)
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                return RunView.of(run)
            now = await db_now(session)
            if run.pending_approval_id is not None:
                kind, actor_id = actor_ref(actor)
                await cancel_approval(
                    session, run.pending_approval_id, actor_type=kind, actor_id=actor_id
                )
            run.status = RunStatus.CANCELLED
            run.updated_at = now
            run.completed_at = now
            run.pending_approval_id = None
            await session.flush()
            await self._audit(session, run, RunActions.CANCELLED, actor=actor)
            return RunView.of(run)

    async def get_run(self, session: AsyncSession, actor: Actor, run_id: uuid.UUID) -> RunView:
        """A run, if the actor may read its graph (404 otherwise)."""
        run = await session.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id))
        if run is None:
            raise RunNotFoundError()
        spec = self._graphs.get((run.workflow, run.graph_name))
        if spec is None or not actor.has(spec.read_permission):
            raise RunNotFoundError()
        return RunView.of(run)

    async def resumable_runs(self, org_id: uuid.UUID) -> list[uuid.UUID]:
        """Runs of ``org_id`` that wait for an approval which is already decided."""
        async with self._session(org_id) as session, session.begin():
            rows = await session.scalars(
                select(WorkflowRun.id)
                .join(Approval, Approval.id == WorkflowRun.pending_approval_id)
                .where(
                    WorkflowRun.status == RunStatus.WAITING_APPROVAL,
                    Approval.status.in_([s.value for s in DECIDED_STATUSES]),
                )
                .order_by(WorkflowRun.updated_at)
                .limit(500)
            )
            return list(rows)

    async def resume_decided_runs(self, org_id: uuid.UUID) -> int:
        """Reconciliation: resume every decided run of one organisation. Returns how many."""
        resumed = 0
        for run_id in await self.resumable_runs(org_id):
            try:
                await self.resume_run(org_id, run_id)
                resumed += 1
            except ResumeRefusedError:
                continue  # another process took it
        return resumed

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _resume_value(approval_id: uuid.UUID) -> Command[Any]:
        return Command(resume={"kind": INTERRUPT_KIND, "approval_id": str(approval_id)})

    async def _claim(
        self, org_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[WorkflowRun, uuid.UUID] | str:
        async with self._session(org_id) as session, session.begin():
            run = await session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
            )
            if run is None:
                return "run_not_found"
            if run.status != RunStatus.WAITING_APPROVAL or run.pending_approval_id is None:
                return "run_not_waiting"
            if (
                run.thread_id != thread_id_for(org_id, run.id)
                or org_of_thread(run.thread_id) != org_id
            ):
                return "thread_not_bound_to_org"
            approval = await session.scalar(
                select(Approval).where(Approval.id == run.pending_approval_id)
            )
            if approval is None or approval.run_id != run.id:
                return "approval_not_of_run"
            if approval.status not in DECIDED_STATUSES:
                return "approval_not_decided"
            if (run.workflow, run.graph_name) not in self._graphs:
                return "graph_not_registered"
            if self._graphs[(run.workflow, run.graph_name)].version != run.graph_version:
                return "graph_version_changed"
            now = await db_now(session)
            run.status = RunStatus.RUNNING
            run.claimed_at = now
            run.updated_at = now
            run.pending_approval_id = None
            await session.flush()
            await self._audit(
                session,
                run,
                RunActions.RESUMED,
                {"approval_id": str(approval.id), "approval_status": approval.status},
            )
            return run, approval.id

    async def _refused(self, org_id: uuid.UUID, run_id: uuid.UUID, reason: str) -> None:
        log.warning(
            "workflow_resume_refused", org_id=str(org_id), run_id=str(run_id), reason=reason
        )
        async with self._session(org_id) as session, session.begin():
            run = await session.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id))
            if run is not None:
                await self._audit(session, run, RunActions.RESUME_REFUSED, {"reason": reason})

    async def _drive(self, run: WorkflowRun, spec: GraphSpec, value: Any) -> RunView:
        if org_of_thread(run.thread_id) != run.org_id:
            raise TenantError("run thread is not bound to its organisation")
        config: RunnableConfig = {"configurable": {"thread_id": run.thread_id}}
        context = self._context(run, spec)
        for _ in range(_MAX_INLINE_RESUMES + 1):
            try:
                async with self.checkpointer.bind(run.org_id) as saver:
                    graph = spec.build().compile(checkpointer=saver)
                    await graph.ainvoke(value, config, context=context)
                    snapshot = await graph.aget_state(config)
            except Exception as exc:
                log.warning(
                    "workflow_run_failed",
                    run_id=str(run.id),
                    error=type(exc).__name__,
                )
                return await self._finish(run, RunStatus.FAILED, _error_text(exc))
            if not snapshot.interrupts:
                if snapshot.next:
                    return await self._finish(run, RunStatus.FAILED, "run_stalled")
                return await self._finish(run, RunStatus.COMPLETED, None)
            approval_id = _approval_from_interrupts(snapshot.interrupts)
            if approval_id is None:
                return await self._finish(run, RunStatus.FAILED, "interrupt_without_approval")
            view, decided = await self._mark_waiting(run, approval_id)
            if not decided:
                return view
            claimed = await self._claim(run.org_id, run.id)
            if isinstance(claimed, str):
                return view
            run, approval_id = claimed
            value = self._resume_value(approval_id)
        return await self._load(run.org_id, run.id)

    async def _mark_waiting(self, run: WorkflowRun, approval_id: uuid.UUID) -> tuple[RunView, bool]:
        async with self._session(run.org_id) as session, session.begin():
            row = await session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run.id).with_for_update()
            )
            if row is None:
                raise RunNotFoundError()
            if row.status != RunStatus.RUNNING:
                return RunView.of(row), False  # cancelled meanwhile
            approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
            if approval is None or approval.run_id != row.id:
                error = "interrupt_for_unknown_approval"
                row.status = RunStatus.FAILED
                row.error = error
                row.completed_at = row.updated_at = await db_now(session)
                await session.flush()
                await self._audit(session, row, RunActions.FAILED, {"error": error})
                return RunView.of(row), False
            row.status = RunStatus.WAITING_APPROVAL
            row.pending_approval_id = approval.id
            row.claimed_at = None
            row.updated_at = await db_now(session)
            await session.flush()
            await self._audit(session, row, RunActions.WAITING, {"approval_id": str(approval.id)})
            return RunView.of(row), approval.status in DECIDED_STATUSES

    async def _finish(self, run: WorkflowRun, status: RunStatus, error: str | None) -> RunView:
        async with self._session(run.org_id) as session, session.begin():
            now = await db_now(session)
            result = await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run.id, WorkflowRun.status == RunStatus.RUNNING)
                .values(
                    status=status, error=error, updated_at=now, completed_at=now, claimed_at=None
                )
                .execution_options(synchronize_session=False)
            )
            row = await session.scalar(
                select(WorkflowRun)
                .where(WorkflowRun.id == run.id)
                .execution_options(populate_existing=True)
            )
            if row is None:  # pragma: no cover - RLS shows the run to its own org
                raise RunNotFoundError()
            if getattr(result, "rowcount", 0) == 1:
                action = (
                    RunActions.COMPLETED if status is RunStatus.COMPLETED else RunActions.FAILED
                )
                await self._audit(session, row, action, {"error": error} if error else None)
            return RunView.of(row)

    async def _load(self, org_id: uuid.UUID, run_id: uuid.UUID) -> RunView:
        async with self._session(org_id) as session, session.begin():
            row = await session.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id))
            if row is None:
                raise RunNotFoundError()
            return RunView.of(row)

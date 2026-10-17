# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Workflow runtime, approvals, executor and SLA against real Postgres (WP1.8).

Uses the tiny ``testwf`` expense workflow from ``wf_support``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from langgraph.types import Command
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.approvals.executor import ExecutorRefusedError, execute_final_action
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalStep,
    Decision,
    DelegationKind,
)
from pairly_core.approvals.service import (
    AlreadyDecidedError,
    NotAssignedError,
    SegregationOfDutiesError,
    create_delegation,
    db_now,
    decide,
    list_inbox,
)
from pairly_core.db import TenantError, create_sessionmaker
from pairly_core.rbac import PermissionDeniedError, SecondFactorRequiredError
from pairly_core.workflow.checkpoint import (
    CHECKPOINT_SCHEMA,
    CheckpointSchemaError,
    psycopg_conninfo,
)
from pairly_core.workflow.models import RunStatus, WorkflowRun, thread_id_for
from pairly_core.workflow.runtime import ResumeRefusedError, WorkflowRuntime
from pairly_worker.workflow_jobs import escalate_overdue_approvals, resume_decided_runs

from .conftest import DbUrls
from .wf_support import (
    EXECUTED,
    REGISTRY,
    SPEC,
    World,
    audit_actions,
    decide_as,
    org_session,
    other_world,
    owner_sql,
    runtime,
    start,
    wf_engine,
    world,
)

__all__ = ["other_world", "runtime", "wf_engine", "world"]  # fixtures


async def _approval(runtime: WorkflowRuntime, world: World, approval_id: Any) -> Approval:
    async with org_session(runtime, world.org_id) as s, s.begin():
        found = await s.scalar(select(Approval).where(Approval.id == uuid.UUID(str(approval_id))))
        assert found is not None
        return found


async def _steps(runtime: WorkflowRuntime, world: World, approval_id: Any) -> list[ApprovalStep]:
    async with org_session(runtime, world.org_id) as s, s.begin():
        rows = await s.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.approval_id == uuid.UUID(str(approval_id)))
            .order_by(ApprovalStep.position)
        )
        return list(rows)


# ------------------------------------------------------------------ happy paths


async def test_propose_interrupt_approve_execute(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world, "120")
    assert run.status == RunStatus.WAITING_APPROVAL
    assert run.thread_id == thread_id_for(world.org_id, run.id)
    approval = await _approval(runtime, world, run.pending_approval_id)
    assert approval.chain_name == "small"
    assert approval.status == "pending"
    assert approval.payload == {"amount": "120", "note": "team lunch"}
    (step,) = await _steps(runtime, world, approval.id)
    approvers = {"appr1", "appr2", "dual", "clerk_approver"}
    assert set(step.assignee_ids) == {world.id(n) for n in approvers}
    assert step.due_at is not None
    assert EXECUTED == []

    outcome = await decide_as(runtime, world, "appr1", approval.id)
    assert outcome.approval_status == "approved"
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.status == RunStatus.COMPLETED
    assert [c.payload["amount"] for c in EXECUTED] == ["120"]
    assert EXECUTED[0].approver_ids == (world.id("appr1"),)
    assert (await _approval(runtime, world, approval.id)).executed_at is not None

    actions = await audit_actions(owner_engine, world.org_id)
    for expected in (
        "core.policy.version_published",
        "core.workflow.run_started",
        "core.approval.requested",
        "core.approval.step_activated",
        "core.workflow.run_waiting",
        "core.approval.decided",
        "core.approval.approved",
        "core.workflow.run_resumed",
        "core.workflow.action_executed",
        "core.workflow.run_completed",
    ):
        assert expected in actions
    # Every run event names the run.
    rows = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT DISTINCT entity_id FROM audit_events WHERE action LIKE 'core.workflow.%'",
    )
    assert {r.entity_id for r in rows} == {str(run.id)}


async def test_reject_path(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world)
    await decide_as(
        runtime, world, "appr2", run.pending_approval_id, Decision.REJECT, reason="no receipt"
    )
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.status == RunStatus.COMPLETED
    assert EXECUTED == []
    assert (await _approval(runtime, world, run.pending_approval_id)).status == "rejected"


async def test_request_changes_creates_a_new_revision(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world)
    first = run.pending_approval_id
    await decide_as(
        runtime, world, "appr1", first, Decision.REQUEST_CHANGES, reason="add the receipt"
    )
    again = await runtime.resume_run(world.org_id, run.id)
    assert again.status == RunStatus.WAITING_APPROVAL
    assert again.pending_approval_id not in {None, first}
    second = await _approval(runtime, world, again.pending_approval_id)
    assert (second.revision, second.status) == (2, "pending")
    assert (await _approval(runtime, world, first)).status == "changes_requested"
    await decide_as(runtime, world, "appr2", second.id)
    assert (await runtime.resume_run(world.org_id, run.id)).status == RunStatus.COMPLETED
    assert len(EXECUTED) == 1


async def test_reason_required_for_reject(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world)
    with pytest.raises(Exception, match="reason is required"):
        await decide_as(runtime, world, "appr1", run.pending_approval_id, Decision.REJECT)


# ------------------------------------------------------------------ resume only after a decision


async def test_resume_refused_without_decision(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    with pytest.raises(ResumeRefusedError) as exc:
        await runtime.resume_run(world.org_id, run.id)
    assert exc.value.reason == "approval_not_decided"
    assert "core.workflow.resume_refused" in await audit_actions(owner_engine, world.org_id)
    assert (await runtime._load(world.org_id, run.id)).status == RunStatus.WAITING_APPROVAL
    with pytest.raises(ResumeRefusedError):
        await runtime.resume_run(world.org_id, uuid.uuid4())


async def test_forged_resume_on_the_graph_waits_again(
    runtime: WorkflowRuntime, world: World
) -> None:
    """Code that bypasses the runtime and resumes the graph with a forged value gets
    nothing: the waiting node reads the database and interrupts again."""
    run = await start(runtime, world)
    async with org_session(runtime, world.org_id) as s, s.begin():
        row = await s.scalar(select(WorkflowRun).where(WorkflowRun.id == run.id))
    assert row is not None
    async with runtime.checkpointer.bind(world.org_id) as saver:
        graph = SPEC.build().compile(checkpointer=saver)
        config: Any = {"configurable": {"thread_id": run.thread_id}}
        forged = {"kind": "pairly.approval", "approval_id": str(run.pending_approval_id)}
        context = runtime._context(row, SPEC)
        await graph.ainvoke(
            Command(resume=forged | {"outcome": "approved"}), config, context=context
        )
        snapshot = await graph.aget_state(config)
    assert snapshot.interrupts
    assert EXECUTED == []
    assert (await _approval(runtime, world, run.pending_approval_id)).status == "pending"


# ------------------------------------------------------------------ executor re-check


async def test_executor_refuses_payload_changed_after_approval(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world, "120")
    await decide_as(runtime, world, "appr1", run.pending_approval_id)
    # Someone edits the run state after the humans approved.
    loaded = await runtime._load(world.org_id, run.id)
    async with runtime.checkpointer.bind(world.org_id) as saver:
        graph = SPEC.build().compile(checkpointer=saver)
        await graph.aupdate_state(
            {"configurable": {"thread_id": loaded.thread_id}}, {"amount": "99999"}
        )
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.status == RunStatus.FAILED
    assert done.error == "executor_refused: payload_hash_mismatch"
    assert EXECUTED == []
    assert "core.workflow.action_refused" in await audit_actions(owner_engine, world.org_id)


async def test_executor_refuses_status_forged_without_decisions(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE approvals SET status = 'approved', current_step = NULL WHERE id = :i",
        {"i": run.pending_approval_id},
    )
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.status == RunStatus.FAILED
    assert done.error == "executor_refused: step_not_complete"
    assert EXECUTED == []


async def test_executor_refuses_when_approver_lost_the_role(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    await decide_as(runtime, world, "appr1", run.pending_approval_id)
    await owner_sql(
        owner_engine,
        world.org_id,
        "DELETE FROM memberships WHERE user_id = :u",
        {"u": world.id("appr1")},
    )
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.error == "executor_refused: approver_lost_permission"
    assert EXECUTED == []


async def test_executor_refuses_other_run_step_or_action(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world)
    other = await start(runtime, world)
    await decide_as(runtime, world, "appr1", run.pending_approval_id)
    payload = {"amount": "120", "note": "team lunch"}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"run_id": other.id}, "approval_for_another_run_or_step"),
        ({"step_key": "other"}, "approval_for_another_run_or_step"),
        ({"action": "nope"}, "unknown_action"),
        ({"approval_id": other.pending_approval_id, "run_id": other.id}, "approval_not_approved"),
    ]
    for override, reason in cases:
        args: dict[str, Any] = {
            "action": "pay",
            "org_id": world.org_id,
            "run_id": run.id,
            "step_key": "pay",
            "approval_id": run.pending_approval_id,
            "payload": payload,
        } | override
        async with org_session(runtime, world.org_id) as s, s.begin():
            with pytest.raises(ExecutorRefusedError) as exc:
                await execute_final_action(s, REGISTRY, SPEC, **args)
        assert exc.value.reason == reason
    # The run is waiting (not executing), so even the right approval is refused here.
    async with org_session(runtime, world.org_id) as s, s.begin():
        with pytest.raises(ExecutorRefusedError, match="run_not_executing"):
            await execute_final_action(
                s,
                REGISTRY,
                SPEC,
                action="pay",
                org_id=world.org_id,
                run_id=run.id,
                step_key="pay",
                approval_id=run.pending_approval_id,
                payload=payload,
            )
    assert EXECUTED == []


# ------------------------------------------------------------------ who may decide


async def test_agent_proposes_but_cannot_approve(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world, by="agent")
    assert run.status == RunStatus.WAITING_APPROVAL
    approval = await _approval(runtime, world, run.pending_approval_id)
    assert approval.submitted_by_type == "agent"
    async with org_session(runtime, world.org_id) as s, s.begin():
        with pytest.raises(PermissionDeniedError):
            await decide(
                s,
                world.agent(),
                approval.id,
                decision=Decision.APPROVE,
                reason=None,
                idempotency_key="agent-key-1",
            )


async def test_step_up_required(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world)
    with pytest.raises(SecondFactorRequiredError):
        await decide_as(runtime, world, "appr1", run.pending_approval_id, otp=False)


async def test_segregation_of_duties_submitter_and_preparer(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world, by="clerk_approver", preparers=["appr2"])
    (step,) = await _steps(runtime, world, run.pending_approval_id)
    assert world.id("clerk_approver") not in step.assignee_ids
    assert world.id("appr2") not in step.assignee_ids
    for name in ("clerk_approver", "appr2"):
        with pytest.raises(SegregationOfDutiesError):
            await decide_as(runtime, world, name, run.pending_approval_id)
    with pytest.raises(PermissionDeniedError):  # holds no testwf approve permission
        await decide_as(runtime, world, "payonly", run.pending_approval_id)
    await decide_as(runtime, world, "appr1", run.pending_approval_id)


async def test_all_of_chain_and_no_two_steps(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world, "5000")
    approval_id = run.pending_approval_id
    approval = await _approval(runtime, world, approval_id)
    assert approval.chain_name == "large"
    _manager, finance = await _steps(runtime, world, approval_id)
    assert set(finance.assignee_ids) == {world.id("fc1"), world.id("fc2"), world.id("dual")}
    with pytest.raises(NotAssignedError):  # finance step is not active yet
        await decide_as(runtime, world, "fc1", approval_id)

    first = await decide_as(runtime, world, "dual", approval_id)
    assert (first.approval_status, first.step_name) == ("pending", "manager")
    with pytest.raises(SegregationOfDutiesError):
        await decide_as(runtime, world, "dual", approval_id)
    assert (await decide_as(runtime, world, "fc1", approval_id)).approval_status == "pending"
    with pytest.raises(AlreadyDecidedError):
        await decide_as(runtime, world, "fc1", approval_id)
    # all_of: dual is excused (decided step 1), fc2 is still required.
    final = await decide_as(runtime, world, "fc2", approval_id)
    assert final.approval_status == "approved"
    assert (await runtime.resume_run(world.org_id, run.id)).status == RunStatus.COMPLETED
    assert set(EXECUTED[0].approver_ids) == {world.id("dual"), world.id("fc1"), world.id("fc2")}


async def test_any_of_needs_one(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world, "999")
    outcome = await decide_as(runtime, world, "appr2", run.pending_approval_id)
    assert outcome.approval_status == "approved"
    with pytest.raises(Exception, match="no longer pending"):
        await decide_as(runtime, world, "appr1", run.pending_approval_id)


# ------------------------------------------------------------------ delegation


async def _delegate(
    runtime: WorkflowRuntime,
    world: World,
    frm: str,
    to: str,
    *,
    kind: DelegationKind = DelegationKind.OUT_OF_OFFICE,
    past: bool = False,
) -> None:
    async with org_session(runtime, world.org_id) as s, s.begin():
        now = await db_now(s)
        start_at = now - timedelta(days=3) if past else now - timedelta(hours=1)
        end_at = now - timedelta(days=1) if past else now + timedelta(days=2)
        await create_delegation(
            s,
            world.actor(frm),
            delegate_id=world.id(to),
            kind=kind,
            starts_at=start_at,
            ends_at=end_at,
        )


async def test_out_of_office_delegate_decides_on_behalf(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world)
    await _delegate(runtime, world, "appr1", "fc1")
    async with org_session(runtime, world.org_id) as s, s.begin():
        page = await list_inbox(s, world.actor("fc1"), assigned_to_me=True)
    assert [i.approval.id for i in page.items] == [run.pending_approval_id]
    outcome = await decide_as(runtime, world, "fc1", run.pending_approval_id)
    assert outcome.approval_status == "approved"
    async with org_session(runtime, world.org_id) as s, s.begin():
        row = await s.scalar(
            select(ApprovalDecision).where(ApprovalDecision.approval_id == run.pending_approval_id)
        )
        assert row is not None
        assert row.on_behalf_of == world.id("appr1")
    assert (await runtime.resume_run(world.org_id, run.id)).status == RunStatus.COMPLETED


async def test_expired_delegation_gives_nothing(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world)
    await _delegate(runtime, world, "appr1", "fc1", kind=DelegationKind.DELEGATION, past=True)
    with pytest.raises(NotAssignedError):
        await decide_as(runtime, world, "fc1", run.pending_approval_id)


async def test_delegation_inside_all_of(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world, "5000")
    approval_id = run.pending_approval_id
    await decide_as(runtime, world, "appr1", approval_id)
    await _delegate(runtime, world, "fc2", "appr2", kind=DelegationKind.DELEGATION)
    await decide_as(runtime, world, "fc1", approval_id)
    assert (await decide_as(runtime, world, "dual", approval_id)).approval_status == "pending"
    final = await decide_as(runtime, world, "appr2", approval_id)  # on behalf of fc2
    assert final.approval_status == "approved"
    assert (await runtime.resume_run(world.org_id, run.id)).status == RunStatus.COMPLETED


async def test_delegate_who_decided_another_step_is_refused(
    runtime: WorkflowRuntime, world: World
) -> None:
    run = await start(runtime, world, "5000")
    await decide_as(runtime, world, "appr2", run.pending_approval_id)
    await _delegate(runtime, world, "fc2", "appr2", kind=DelegationKind.DELEGATION)
    with pytest.raises(SegregationOfDutiesError):
        await decide_as(runtime, world, "appr2", run.pending_approval_id)


# ------------------------------------------------------------------ SLA and reconciliation


async def test_sla_escalation_job(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE approval_steps SET warn_at = now() - interval '6 hours', "
        "due_at = now() - interval '2 hours' WHERE approval_id = :a",
        {"a": run.pending_approval_id},
    )
    async with org_session(runtime, world.org_id) as s, s.begin():
        overdue = await list_inbox(s, world.actor("appr1"), sla="overdue")
        on_track = await list_inbox(s, world.actor("appr1"), sla="on_track")
    assert run.pending_approval_id in [i.approval.id for i in overdue.items]
    assert run.pending_approval_id not in [i.approval.id for i in on_track.items]
    assert overdue.items[0].sla == "overdue"

    ctx: dict[str, Any] = {
        "sessionmaker": create_sessionmaker(wf_engine),
        "registry": REGISTRY,
        "runtime": runtime,
    }
    summary = await escalate_overdue_approvals(ctx)
    assert summary["escalated"] >= 1
    (step,) = await _steps(runtime, world, run.pending_approval_id)
    assert step.escalated_at is not None
    assert set(step.escalation_ids) == {world.id("fc1"), world.id("fc2")}
    assert "core.approval.escalated" in await audit_actions(owner_engine, world.org_id)
    # Escalated once only.
    await escalate_overdue_approvals(ctx)
    (again,) = await _steps(runtime, world, run.pending_approval_id)
    assert again.escalation_level == 1
    # An escalation approver may now decide.
    assert (await decide_as(runtime, world, "fc2", run.pending_approval_id)).approval_status == (
        "approved"
    )


async def test_reconciliation_job_resumes_decided_runs(
    runtime: WorkflowRuntime, world: World, wf_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    await decide_as(runtime, world, "appr1", run.pending_approval_id)
    ctx: dict[str, Any] = {"sessionmaker": create_sessionmaker(wf_engine), "runtime": runtime}
    summary = await resume_decided_runs(ctx)
    assert summary["resumed"] >= 1
    assert (await runtime._load(world.org_id, run.id)).status == RunStatus.COMPLETED
    assert [c.run_id for c in EXECUTED if c.org_id == world.org_id] == [run.id]


async def test_cancel_run_cancels_the_approval(runtime: WorkflowRuntime, world: World) -> None:
    run = await start(runtime, world)
    cancelled = await runtime.cancel_run(world.actor("clerk"), run.id)
    assert cancelled.status == RunStatus.CANCELLED
    assert (await _approval(runtime, world, run.pending_approval_id)).status == "cancelled"
    with pytest.raises(ResumeRefusedError):
        await runtime.resume_run(world.org_id, run.id)


# ------------------------------------------------------------------ tenant isolation


async def test_runs_approvals_and_checkpoints_are_tenant_bound(
    runtime: WorkflowRuntime, world: World, other_world: World, migrated: DbUrls
) -> None:
    run = await start(runtime, world)
    # Another org cannot see, decide, resume or read the checkpoints of this run.
    async with org_session(runtime, other_world.org_id) as s, s.begin():
        assert (
            await s.scalar(select(Approval).where(Approval.id == run.pending_approval_id)) is None
        )
        assert (await list_inbox(s, other_world.actor("appr1"))).items == []
    with pytest.raises(Exception, match="not found"):
        await decide_as(runtime, other_world, "appr1", run.pending_approval_id)
    with pytest.raises(ResumeRefusedError, match="run_not_found"):
        await runtime.resume_run(other_world.org_id, run.id)

    async with runtime.checkpointer.bind(other_world.org_id) as saver:
        with pytest.raises(TenantError):
            await saver.aget_tuple({"configurable": {"thread_id": run.thread_id}})
        # Raw SQL on the same bound connection sees no foreign rows and cannot write any.
        cursor = await saver.connection.execute("SELECT count(*) AS n FROM checkpoints")
        assert (await cursor.fetchone())["n"] == 0  # type: ignore[index]
        with pytest.raises(CheckViolation):
            await saver.connection.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) "
                "VALUES (%s, 'x', '{}'::jsonb)",
                (run.thread_id,),
            )
    async with runtime.checkpointer.bind(world.org_id) as saver:
        cursor = await saver.connection.execute(
            "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (run.thread_id,)
        )
        assert (await cursor.fetchone())["n"] > 0  # type: ignore[index]

    # Without an org on the connection: nothing visible, nothing writable.
    conn = await AsyncConnection.connect(
        psycopg_conninfo(migrated.app),
        autocommit=True,
        options=f"-c search_path={CHECKPOINT_SCHEMA}",
    )
    try:
        plain = await conn.execute("SELECT count(*) FROM checkpoints")
        assert (await plain.fetchone()) == (0,)
        with pytest.raises(InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) "
                "VALUES (%s, 'y', '{}'::jsonb)",
                (run.thread_id,),
            )
    finally:
        await conn.close()


async def test_checkpoint_schema_check(
    runtime: WorkflowRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.check()
    from pairly_core.workflow import checkpoint

    monkeypatch.setattr(checkpoint, "APPLIED_CHECKPOINT_MIGRATION", 8)
    with pytest.raises(CheckpointSchemaError):
        await runtime.check()


async def test_new_tables_force_rls(owner_engine: AsyncEngine) -> None:
    async with owner_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'r' AND c.relname IN ('policy_versions', 'workflow_runs', "
                    "'approvals', 'approval_steps', 'approval_decisions', 'approval_delegations', "
                    "'approval_links', 'checkpoints', 'checkpoint_blobs', 'checkpoint_writes')"
                )
            )
        ).all()
    assert len(rows) == 10
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows)


async def test_decisions_and_policies_are_append_only(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    run = await start(runtime, world)
    await decide_as(runtime, world, "appr1", run.pending_approval_id)
    for sql in (
        "UPDATE approval_decisions SET reason = 'x'",
        "DELETE FROM approval_decisions",
        "UPDATE policy_versions SET content = 'x'",
    ):
        with pytest.raises(Exception, match="append-only"):
            await owner_sql(owner_engine, world.org_id, sql)


@pytest.mark.parametrize(
    ("decider", "behalf", "second_factor", "reason"),
    [
        ("clerk", None, True, "segregation_of_duties"),
        ("appr1", None, False, "decision_without_permission_or_second_factor"),
        ("fc1", None, True, "decider_not_assigned"),
        ("fc1", "appr1", True, "delegation_not_covering_decision"),
    ],
)
async def test_executor_rederives_the_approval_from_decision_rows(
    runtime: WorkflowRuntime,
    world: World,
    owner_engine: AsyncEngine,
    decider: str,
    behalf: str | None,
    second_factor: bool,
    reason: str,
) -> None:
    """Rows forged straight into the database (bypassing ``decide``) never pass."""
    run = await start(runtime, world)
    (step,) = await _steps(runtime, world, run.pending_approval_id)
    await owner_sql(
        owner_engine,
        world.org_id,
        "INSERT INTO approval_decisions (org_id, approval_id, step_id, actor_user_id, "
        "on_behalf_of, via_escalation, decision, channel, permission, second_factor, "
        "request_hash) VALUES (:o, :a, :s, :u, :b, false, 'approve', 'web', "
        "'testwf.expense.approve', :f, repeat('0', 64))",
        {
            "o": world.org_id,
            "a": run.pending_approval_id,
            "s": step.id,
            "u": world.id(decider),
            "b": world.id(behalf) if behalf else None,
            "f": second_factor,
        },
    )
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE approvals SET status = 'approved', current_step = NULL WHERE id = :i",
        {"i": run.pending_approval_id},
    )
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.error == f"executor_refused: {reason}"
    assert EXECUTED == []

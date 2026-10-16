# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Attempts from the October 2026 security review (``docs/security-review-2026-10.md``)
against the approvals engine: insider approval abuse through delegation, actors that
are not people, and races. Each test names the finding or the control it attacks."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.approvals.models import ApprovalDecision, Decision, DelegationKind
from pairly_core.approvals.service import (
    ApprovalStateError,
    NotAssignedError,
    SegregationOfDutiesError,
    create_delegation,
    db_now,
    decide,
)
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError
from pairly_core.workflow.runtime import ResumeRefusedError, WorkflowRuntime

from .wf_support import (
    EXECUTED,
    PERMISSION,
    World,
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


async def _delegate(runtime: WorkflowRuntime, world: World, frm: str, to: str) -> None:
    async with org_session(runtime, world.org_id) as s, s.begin():
        now = await db_now(s)
        await create_delegation(
            s,
            world.actor(frm),
            delegate_id=world.id(to),
            kind=DelegationKind.DELEGATION,
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=2),
        )


async def _remove(owner_engine: AsyncEngine, world: World, name: str) -> None:
    await owner_sql(
        owner_engine,
        world.org_id,
        "DELETE FROM memberships WHERE user_id = :u",
        {"u": world.id(name)},
    )


async def test_the_submitter_cannot_approve_as_a_delegate(
    runtime: WorkflowRuntime, world: World
) -> None:
    """Self-approval through delegation: an approver delegates to the person who
    submitted the item; the submitter then decides on the approver's seat."""
    EXECUTED.clear()
    run = await start(runtime, world, by="clerk_approver")
    await _delegate(runtime, world, "appr1", "clerk_approver")
    with pytest.raises(SegregationOfDutiesError):
        await decide_as(runtime, world, "clerk_approver", run.pending_approval_id)


async def test_a_delegation_dies_with_the_delegators_role(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    """M-01. ``fc1`` holds the approve permission but is not an approver of the small
    chain. ``appr1`` delegates to ``fc1`` and then leaves the organisation. The
    delegation must not keep ``appr1``'s seat alive for ``fc1``: neither the decision
    nor the final action may count it."""
    EXECUTED.clear()
    run = await start(runtime, world)
    await _delegate(runtime, world, "appr1", "fc1")
    await _remove(owner_engine, world, "appr1")
    with pytest.raises(NotAssignedError):
        await decide_as(runtime, world, "fc1", run.pending_approval_id)
    assert EXECUTED == []


async def test_the_executor_refuses_a_seat_whose_holder_lost_the_role(
    runtime: WorkflowRuntime, world: World, owner_engine: AsyncEngine
) -> None:
    """M-01, second layer: the delegate decided while the delegator was an approver,
    then the delegator lost the role before the final action ran."""
    EXECUTED.clear()
    run = await start(runtime, world)
    await _delegate(runtime, world, "appr1", "fc1")
    outcome = await decide_as(runtime, world, "fc1", run.pending_approval_id)
    assert outcome.approval_status == "approved"
    await _remove(owner_engine, world, "appr1")
    done = await runtime.resume_run(world.org_id, run.id)
    assert done.error == "executor_refused: approver_lost_permission"
    assert EXECUTED == []


@pytest.mark.parametrize("kind", [ActorKind.API_KEY, ActorKind.AGENT])
async def test_keys_and_agents_never_decide(
    runtime: WorkflowRuntime, world: World, kind: ActorKind
) -> None:
    """Approving with an API key or through MCP (an agent), even one that somehow holds
    the approve permission and a second factor."""
    run = await start(runtime, world)
    forged = Actor(
        kind=kind,
        id=uuid.uuid4(),
        org_id=world.org_id,
        grants=(),
        permissions=frozenset({PERMISSION}),
        amr=frozenset({"otp"}),
    )
    async with org_session(runtime, world.org_id) as s, s.begin():
        with pytest.raises(PermissionDeniedError):
            await decide(
                s,
                forged,
                run.pending_approval_id,
                decision=Decision.APPROVE,
                reason=None,
                idempotency_key="k-forged",
            )


async def test_two_approvers_racing_record_one_decision(
    runtime: WorkflowRuntime, world: World
) -> None:
    """Racing two approvals of an any-of step: exactly one decision, one execution."""
    EXECUTED.clear()
    run = await start(runtime, world)

    async def attempt(name: str) -> Any:
        try:
            return await decide_as(runtime, world, name, run.pending_approval_id)
        except ApprovalStateError as exc:
            return exc

    results = await asyncio.gather(attempt("appr1"), attempt("appr2"), attempt("dual"))
    decided = [r for r in results if not isinstance(r, Exception)]
    assert len(decided) == 1
    async with org_session(runtime, world.org_id) as s, s.begin():
        rows = list(
            await s.scalars(
                select(ApprovalDecision).where(
                    ApprovalDecision.approval_id == run.pending_approval_id
                )
            )
        )
    assert len(rows) == 1
    await runtime.resume_run(world.org_id, run.id)
    with pytest.raises(ResumeRefusedError):
        await runtime.resume_run(world.org_id, run.id)
    assert len(EXECUTED) == 1

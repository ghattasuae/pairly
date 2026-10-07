# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Support for the workflow and approvals tests (WP1.8).

A tiny test workflow (``testwf``), declared like a plugin, with one graph ``expense``:

    propose -> wait --approved--> execute -> END
                    --rejected--> rejected -> END
                    --changes_requested--> revise -> propose (revision + 1)
                    --(no decision)--> wait (interrupt again)

Approval rules: amounts up to 1000 need one approver (any_of); larger amounts need an
approver, then every finance controller (all_of). Escalation goes to finance controllers.

``world`` seeds one organisation with named people. Organisations are never deleted:
decisions and policy versions are append-only, and the test database is throwaway.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from pairly_core.approvals.models import Decision
from pairly_core.approvals.service import DecisionOutcome, decide
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import (
    Actor,
    ActorKind,
    PermissionSpec,
    RoleGrant,
    RoleRegistry,
    agent_actor,
    build_registry,
)
from pairly_core.rbac import BuiltinRole as R
from pairly_core.workflow.checkpoint import TenantCheckpointer
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import WorkflowRuntime, node_retry
from pairly_core.workflow.spec import ApprovalType, FinalActionCall, GraphSpec
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls

WF = "testwf"
APPROVAL_TYPE = "testwf.expense"
PERMISSION = "testwf.expense.approve"

RULES = """\
schema: pairly.approval_rules/v1
approval_type: testwf.expense
sla_hours: 24
warn_hours: 4
escalation:
  roles: [finance_controller]
chains:
  - name: small
    when:
      - {field: amount, op: lte, value: "1000"}
    steps:
      - {name: manager, mode: any_of, roles: [approver]}
  - name: large
    steps:
      - {name: manager, mode: any_of, roles: [approver]}
      - {name: finance, mode: all_of, roles: [finance_controller]}
"""

# Calls of the final action, in order: what really "happened".
EXECUTED: list[FinalActionCall] = []


class ExpenseState(TypedDict, total=False):
    amount: str
    note: str
    preparers: list[str]
    approval_id: str
    revision: int
    outcome: str
    executed: bool


def _payload(state: ExpenseState) -> dict[str, Any]:
    return {"amount": state["amount"], "note": state.get("note", "")}


async def propose(state: ExpenseState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    revision = state.get("revision", 1)
    approval_id = await request_approval(
        runtime.context,
        step_key="pay",
        approval_type=APPROVAL_TYPE,
        title=f"Expense {state['amount']}",
        payload=_payload(state),
        context={"note": state.get("note", ""), "revision": revision},
        revision=revision,
        entity_type="expense",
        entity_id="exp-1",
        preparer_ids=[uuid.UUID(p) for p in state.get("preparers", [])],
    )
    return {"approval_id": approval_id, "revision": revision}


async def wait(state: ExpenseState, runtime: Runtime[WorkflowContext]) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "rejected", "changes_requested": "revise"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def execute(state: ExpenseState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    await run_final_action(
        runtime.context,
        action="pay",
        approval_id=state["approval_id"],
        step_key="pay",
        payload=_payload(state),
    )
    return {"executed": True}


async def rejected(state: ExpenseState) -> dict[str, Any]:
    return {"executed": False}


async def revise(state: ExpenseState) -> dict[str, Any]:
    return {"revision": state.get("revision", 1) + 1}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ExpenseState, context_schema=WorkflowContext)
    graph.add_node("propose", propose, retry_policy=node_retry(), timeout=30)
    graph.add_node("wait", wait, destinations=("wait", "execute", "rejected", "revise"))
    graph.add_node("execute", execute, retry_policy=node_retry(), timeout=30)
    graph.add_node("rejected", rejected)
    graph.add_node("revise", revise)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "wait")
    graph.add_edge("revise", "propose")
    graph.add_edge("execute", END)
    graph.add_edge("rejected", END)
    return graph


async def pay(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    EXECUTED.append(call)
    return {"paid": call.payload["amount"]}


SPEC = GraphSpec(
    workflow=WF,
    name="expense",
    version=1,
    build=build_graph,
    start_permission="testwf.expense.propose",
    read_permission="testwf.expense.read",
    approval_types=(ApprovalType(APPROVAL_TYPE, PERMISSION, RULES, "Expense"),),
    final_actions={"pay": pay},
)


@dataclass(frozen=True, slots=True)
class ExpensePlugin:
    id: str = WF
    name: str = "Test workflow"
    permissions: tuple[PermissionSpec, ...] = (
        PermissionSpec(
            "testwf.expense.read",
            "View expenses",
            frozenset({R.ADMIN, R.AP_CLERK, R.APPROVER, R.FINANCE_CONTROLLER, R.AGENT}),
        ),
        PermissionSpec(
            "testwf.expense.propose", "Propose an expense", frozenset({R.AP_CLERK, R.AGENT})
        ),
        PermissionSpec(
            PERMISSION, "Approve an expense", frozenset({R.APPROVER, R.FINANCE_CONTROLLER})
        ),
    )
    graphs: tuple[GraphSpec, ...] = (SPEC,)


TEST_PLUGIN = ExpensePlugin()
PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin, TEST_PLUGIN]
REGISTRY: RoleRegistry = build_registry(PLUGINS)


# ------------------------------------------------------------------ people


# name -> memberships (role, workflow)
PEOPLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "clerk": (("ap_clerk", None),),
    "clerk_approver": (("ap_clerk", None), ("approver", None)),
    "appr1": (("approver", None),),
    "appr2": (("approver", None),),
    "dual": (("approver", None), ("finance_controller", None)),
    "fc1": (("finance_controller", None),),
    "fc2": (("finance_controller", None),),
    "payonly": (("approver", "pay"),),
    "admin": (("admin", None),),
}


@dataclass(frozen=True)
class Person:
    id: uuid.UUID
    subject: str
    grants: tuple[RoleGrant, ...]


@dataclass
class World:
    org_id: uuid.UUID
    people: dict[str, Person] = field(default_factory=dict)

    def id(self, name: str) -> uuid.UUID:
        return self.people[name].id

    def actor(self, name: str, *, otp: bool = True) -> Actor:
        person = self.people[name]
        return Actor(
            kind=ActorKind.USER,
            id=person.id,
            org_id=self.org_id,
            grants=person.grants,
            permissions=REGISTRY.resolve(person.grants),
            subject=person.subject,
            amr=frozenset({"otp"}) if otp else frozenset(),
        )

    def agent(self) -> Actor:
        return agent_actor(REGISTRY, self.org_id, uuid.uuid4())


async def seed_world(owner: AsyncEngine, label: str) -> World:
    world = World(org_id=uuid.uuid4())
    async with owner.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(world.org_id)}
        )
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": world.org_id, "n": f"WF {label}", "s": f"wf-{label}-{world.org_id.hex[:10]}"},
        )
        for name, memberships in PEOPLE.items():
            user_id = uuid.uuid4()
            subject = f"sub-wf-{name}-{user_id.hex[:10]}"
            await conn.execute(
                text("SELECT set_config('app.auth_subject', :s, true)"), {"s": subject}
            )
            await conn.execute(
                text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
                {"id": user_id, "s": subject, "e": f"{name}-{user_id.hex[:6]}@example.test"},
            )
            for role, workflow in memberships:
                await conn.execute(
                    text(
                        "INSERT INTO memberships (org_id, user_id, role_id, workflow) "
                        "SELECT :o, :u, r.id, :w FROM roles r "
                        "WHERE r.key = :role AND r.org_id IS NULL"
                    ),
                    {"o": world.org_id, "u": user_id, "w": workflow, "role": role},
                )
            world.people[name] = Person(
                id=user_id,
                subject=subject,
                grants=tuple(RoleGrant(role, workflow) for role, workflow in memberships),
            )
    return world


@pytest.fixture
async def wf_engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> World:
    EXECUTED.clear()
    return await seed_world(owner_engine, "a")


@pytest.fixture
async def other_world(owner_engine: AsyncEngine) -> World:
    return await seed_world(owner_engine, "b")


@pytest.fixture
def runtime(wf_engine: AsyncEngine, migrated: DbUrls) -> WorkflowRuntime:
    return WorkflowRuntime(
        sessionmaker=create_sessionmaker(wf_engine),
        checkpointer=TenantCheckpointer(migrated.app),
        registry=REGISTRY,
        graphs=[SPEC],
    )


# ------------------------------------------------------------------ helpers


def org_session(runtime: WorkflowRuntime, org_id: uuid.UUID) -> AsyncSession:
    session = runtime.sessionmaker()
    set_tenant(session, org_id, None)
    return session


async def start(
    runtime: WorkflowRuntime,
    world: World,
    amount: str = "120",
    *,
    by: str = "clerk",
    preparers: list[str] | None = None,
) -> Any:
    actor = world.agent() if by == "agent" else world.actor(by)
    state: dict[str, Any] = {"amount": amount, "note": "team lunch"}
    if preparers:
        state["preparers"] = [str(world.id(p)) for p in preparers]
    return await runtime.start_run(actor, workflow=WF, graph="expense", input=state)


async def decide_as(
    runtime: WorkflowRuntime,
    world: World,
    name: str,
    approval_id: uuid.UUID,
    decision: Decision = Decision.APPROVE,
    *,
    reason: str | None = None,
    otp: bool = True,
    key: str | None = None,
) -> DecisionOutcome:
    actor = world.actor(name, otp=otp)
    async with org_session(runtime, world.org_id) as session, session.begin():
        return await decide(
            session,
            actor,
            approval_id,
            decision=decision,
            reason=reason,
            idempotency_key=key or f"k-{uuid.uuid4().hex}",
        )


async def owner_sql(
    owner: AsyncEngine, org_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def audit_actions(owner: AsyncEngine, org_id: uuid.UUID) -> list[str]:
    rows = await owner_sql(owner, org_id, "SELECT action FROM audit_events ORDER BY seq")
    return [r.action for r in rows]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The run context every graph node receives as ``runtime.context`` (LangGraph Runtime).

It is created by the runtime for one invocation and is never checkpointed. It carries the
organisation and run the node works for, a way to open sessions bound to that
organisation (so nodes never choose a tenant themselves) and the engine services the
process holds (``services``: document store, model router, scanner, parse settings).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.hooks import ApprovalHooks
from pairly_core.audit import ActorType
from pairly_core.db import set_tenant
from pairly_core.rbac import RoleRegistry
from pairly_core.workflow.services import NO_SERVICES, GraphServices
from pairly_core.workflow.spec import GraphSpec


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    org_id: uuid.UUID
    run_id: uuid.UUID
    graph: GraphSpec
    started_by_type: ActorType
    started_by_id: str
    sessionmaker: async_sessionmaker[AsyncSession]
    registry: RoleRegistry
    hooks: ApprovalHooks
    services: GraphServices = NO_SERVICES

    @property
    def workflow(self) -> str:
        return self.graph.workflow

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session in one transaction, bound to the run's organisation. Commits on exit."""
        async with self.sessionmaker() as session:
            set_tenant(session, self.org_id, None)
            async with session.begin():
                yield session

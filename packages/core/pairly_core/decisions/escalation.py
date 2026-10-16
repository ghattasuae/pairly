# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notify the people who may resolve an overdue appeal (WP1.9 notify).

``AppealEscalationNotifier`` is called by ``escalate_overdue_appeals`` for every appeal
it escalates, in the same transaction. Recipients are the organisation's members who
may decide an appeal of that workflow (the workflow's decide-type permission, or the
core-wide ``core.appeal.decide``; ``pairly_core.decisions.scope``), minus the person who
filed the appeal and the person whose decision is appealed (they cannot resolve it).

The ``core.appeal.escalated`` template is not sensitive: it carries ids, the workflow
and the due date only, never the requester's words. Each person gets their own
savepoint, so one failing recipient never blocks the others or the escalation.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.audit import ActorType
from pairly_core.decisions.models import Appeal, DecisionRecord
from pairly_core.decisions.permissions import APPEAL_DECIDE
from pairly_core.decisions.scope import DECIDE_ACTIONS
from pairly_core.models import Membership
from pairly_core.notifications.links import absolute_url, decision_path
from pairly_core.notifications.models import Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.rbac import RoleRegistry, permission_action, permission_namespace

log = structlog.get_logger(__name__)

TEMPLATE_KEY: Final = "core.appeal.escalated"


def appeal_ref(appeal_id: uuid.UUID) -> str:
    return f"APL-{appeal_id.hex[:8].upper()}"


def may_decide(permissions: frozenset[str], workflow: str) -> bool:
    if APPEAL_DECIDE in permissions:
        return True
    return any(
        permission_namespace(p) == workflow and permission_action(p) in DECIDE_ACTIONS
        for p in permissions
    )


async def appeal_deciders(
    session: AsyncSession, registry: RoleRegistry, workflow: str
) -> list[uuid.UUID]:
    """Members of the session's organisation who may decide an appeal in ``workflow``."""
    candidates = set(
        await session.scalars(
            select(Membership.user_id).where(
                or_(Membership.workflow.is_(None), Membership.workflow == workflow)
            )
        )
    )
    perms = await member_permissions(session, registry, candidates)
    return sorted(u for u in candidates if may_decide(perms.get(u, frozenset()), workflow))


@dataclass(frozen=True, slots=True)
class AppealEscalationNotifier:
    registry: RoleRegistry
    settings: NotificationSettings

    async def __call__(self, session: AsyncSession, appeal: Appeal) -> int:
        record = await session.scalar(
            select(DecisionRecord).where(DecisionRecord.id == appeal.decision_id)
        )
        if record is None:  # pragma: no cover - foreign key
            return 0
        excluded = {appeal.filed_by}
        if record.actor_type == ActorType.USER.value:
            with contextlib.suppress(ValueError):
                excluded.add(uuid.UUID(record.actor_id))
        # The decision record, where its appeals are decided.
        path = decision_path(record.id)
        queue_url = absolute_url(self.settings.web_url, path)
        variables = {
            "appeal_ref": appeal_ref(appeal.id),
            "decision_id": record.id,
            "workflow": record.workflow,
            "due_at": appeal.sla_due_at,
            "queue_url": queue_url,
        }
        sent = 0
        for person in await appeal_deciders(session, self.registry, record.workflow):
            if person in excluded:
                continue
            try:
                async with session.begin_nested():
                    await notify(
                        session,
                        recipient=Recipient.user(person),
                        template_key=TEMPLATE_KEY,
                        variables=variables,
                        dedupe_key=f"appeal:{appeal.id}:escalated:{person}",
                        urgency=Urgency.URGENT,
                        extras=Extras(link=path),
                        workflow=record.workflow,
                    )
                sent += 1
            except Exception as exc:
                log.warning(
                    "appeal_notification_failed",
                    appeal_id=str(appeal.id),
                    user_id=str(person),
                    error=type(exc).__name__,
                )
        return sent

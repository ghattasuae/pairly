# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The first-deletion notice: before a subjects rule deletes anything for the first time
in an organisation, its admins hear of it.

``AdminRetentionNotifier`` sends ``core.retention.first_deletion`` (category
``retention``: email and in-app, always urgent, never switched off) to every member who
may change retention (``core.retention.manage``): "Retention will start deleting N
closed candidate records on <date>. Review the list or pause.", with a link to the
Retention settings page (the preview list and the Pause button).

The retention service calls it once per organisation and rule, in the run's plan
transaction, and records the notice on ``retention_rule_controls`` only when at least
one admin got it; until then the rule deletes nothing (``pairly_core.retention.service``).
Each person gets their own savepoint, so one failing recipient never blocks the others.
The message carries a count, a kind of subject and a date, never a name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.models import Membership
from pairly_core.notifications.links import absolute_url, public_web_url
from pairly_core.notifications.models import NotificationStatus, Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import RoleRegistry, build_registry
from pairly_core.retention.permissions import RETENTION_MANAGE

log = structlog.get_logger("pairly_core.retention.notice")

TEMPLATE_KEY: Final = "core.retention.first_deletion"
REVIEW_PATH: Final = "/settings/retention"
_DELIVERED: Final = frozenset(
    {
        NotificationStatus.SENT.value,
        NotificationStatus.QUEUED.value,
        NotificationStatus.DIGEST.value,
    }
)


class RetentionNotifier(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        rule: str,
        workflow: str,
        subject_type: str,
        count: int,
        deletes_on: datetime,
    ) -> int:
        """Tell the organisation's admins; returns how many got the notice."""
        ...


async def retention_admins(session: AsyncSession, registry: RoleRegistry) -> list[uuid.UUID]:
    """Members of the session's organisation who may change retention."""
    members = set(await session.scalars(select(Membership.user_id)))
    perms = await member_permissions(session, registry, members)
    return sorted(u for u in members if RETENTION_MANAGE in perms.get(u, frozenset()))


@dataclass(frozen=True, slots=True)
class AdminRetentionNotifier:
    registry: RoleRegistry
    web_url: str | None = None

    async def __call__(
        self,
        session: AsyncSession,
        *,
        rule: str,
        workflow: str,
        subject_type: str,
        count: int,
        deletes_on: datetime,
    ) -> int:
        variables = {
            "count": count,
            "subject_type": subject_type,
            "deletes_on": deletes_on,
            "review_url": absolute_url(self.web_url, REVIEW_PATH),
        }
        sent = 0
        for person in await retention_admins(session, self.registry):
            try:
                async with session.begin_nested():
                    results = await notify(
                        session,
                        recipient=Recipient.user(person),
                        template_key=TEMPLATE_KEY,
                        variables=variables,
                        dedupe_key=f"retention:{rule}:first_deletion:{person}",
                        channels=("email", "in_app"),
                        urgency=Urgency.URGENT,
                        extras=Extras(link=REVIEW_PATH),
                        workflow=workflow,
                    )
            except Exception as exc:
                log.warning(
                    "retention_notice_failed",
                    rule=rule,
                    user_id=str(person),
                    error=type(exc).__name__,
                )
                continue
            if any(r.status in _DELIVERED for r in results):
                sent += 1
        return sent


def default_notifier() -> AdminRetentionNotifier:
    """Core roles and the public web address from the environment (the worker passes
    its own registry and notification settings instead)."""
    return AdminRetentionNotifier(registry=build_registry(), web_url=public_web_url())

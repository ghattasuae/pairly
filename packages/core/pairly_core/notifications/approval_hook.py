# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Approval notifications: the ``step_activated`` and ``step_escalated`` hooks.

Who is told:

* step activated: every approver of the step. An approver who is out of office is
  replaced by their delegate; a plain delegation tells both.
* step escalated (SLA breach, urgent): the escalation approvers and the step's
  approvers who have not decided yet.

What they get, per channel and preferences (``pairly_core.notifications.preferences``):

* email: Approve and Reject buttons with signed single-use links (``issue_link``,
  ``email_link`` channel) to the web confirmation page ``/approvals/link#<token>``.
* Slack (when the person linked their Slack account and the organisation has Slack):
  a Block Kit message whose Approve and Reject buttons open the same page with
  ``slack_link`` tokens, plus a native "remind me" button.
* in-app: an item linking to the approval in the inbox (``/inbox?item=<id>``, an app
  path; email and Slack carry the same page as a full link).

Links are only issued when the message goes out now. When the email or Slack message is
held (digest, snooze, quiet hours), it carries the inbox link only: a 30 minute link
would be dead by then. People who could not decide anyway (the submitter, a preparer,
someone who already decided) get no links, only the in-app item.

The hook runs inside the approval's transaction. Each person's notifications run in a
savepoint, and a failure is logged and skipped: a broken template or a missing address
never blocks a decision or an escalation (the SLA job and the inbox still show it).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import BaseApprovalHook, StepActivated, StepEscalated
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalStep,
    Channel,
    Decision,
    DelegationKind,
)
from pairly_core.approvals.service import active_delegations, db_now
from pairly_core.errors import BadRequestError
from pairly_core.integrations.slack.blocks import SlackButton, approval_buttons
from pairly_core.integrations.slack.links import active_slack, slack_link_for_user
from pairly_core.notifications.links import absolute_url, approval_path
from pairly_core.notifications.models import NotificationPreference, Urgency
from pairly_core.notifications.preferences import plan, preferences_from_row
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.signed_links import issue_link

log = structlog.get_logger(__name__)

REQUESTED = "core.approval.requested"
ESCALATED = "core.approval.escalated"


def link_url(settings: NotificationSettings, token: str) -> str:
    # The token goes in the fragment: browsers never send it to a server.
    return f"{settings.web_url}/approvals/link#{token}"


class NotificationApprovalHook(BaseApprovalHook):
    def __init__(self, settings: NotificationSettings, *, link_ttl_seconds: int = 1800) -> None:
        self._settings = settings
        self._ttl = link_ttl_seconds

    async def step_activated(self, session: AsyncSession, event: StepActivated) -> None:
        approval = await session.scalar(select(Approval).where(Approval.id == event.approval_id))
        if approval is None:  # pragma: no cover - same transaction
            return
        now = await db_now(session)
        people: list[uuid.UUID] = []
        delegations = await active_delegations(
            session, workflow=approval.workflow, now=now, delegator_ids=event.assignee_ids
        )
        away = {d.delegator_id for d in delegations if d.kind == DelegationKind.OUT_OF_OFFICE}
        for person in event.assignee_ids:
            if person not in away:
                people.append(person)
        people.extend(d.delegate_id for d in delegations)
        await self._notify_all(
            session, approval, event.step_id, event.step_name, event.due_at, people, 0, now
        )

    async def step_escalated(self, session: AsyncSession, event: StepEscalated) -> None:
        approval = await session.scalar(select(Approval).where(Approval.id == event.approval_id))
        step = await session.scalar(select(ApprovalStep).where(ApprovalStep.id == event.step_id))
        if approval is None or step is None:  # pragma: no cover - same transaction
            return
        decided = set(
            await session.scalars(
                select(ApprovalDecision.actor_user_id).where(ApprovalDecision.step_id == step.id)
            )
        )
        people = [*event.escalation_ids, *(p for p in step.assignee_ids if p not in decided)]
        now = await db_now(session)
        await self._notify_all(
            session,
            approval,
            step.id,
            step.name,
            event.due_at,
            people,
            max(step.escalation_level, 1),
            now,
        )

    async def _notify_all(
        self,
        session: AsyncSession,
        approval: Approval,
        step_id: uuid.UUID,
        step_name: str,
        due_at: datetime,
        people: list[uuid.UUID],
        level: int,
        now: datetime,
    ) -> None:
        for person in dict.fromkeys(people):
            try:
                async with session.begin_nested():
                    await self._notify_person(
                        session, approval, step_id, step_name, due_at, person, level, now
                    )
            except Exception as exc:
                log.warning(
                    "approval_notification_failed",
                    approval_id=str(approval.id),
                    user_id=str(person),
                    error=type(exc).__name__,
                )

    async def _links(
        self, session: AsyncSession, approval: Approval, person: uuid.UUID, channel: Channel
    ) -> tuple[str, str] | None:
        """Approve and reject tokens, or None when this person cannot decide the step."""
        try:
            approve = await issue_link(
                session,
                approval_id=approval.id,
                user_id=person,
                action=Decision.APPROVE,
                channel=channel,
                ttl_seconds=self._ttl,
            )
            reject = await issue_link(
                session,
                approval_id=approval.id,
                user_id=person,
                action=Decision.REJECT,
                channel=channel,
                ttl_seconds=self._ttl,
            )
        except BadRequestError:
            return None
        return approve.token, reject.token

    async def _notify_person(
        self,
        session: AsyncSession,
        approval: Approval,
        step_id: uuid.UUID,
        step_name: str,
        due_at: datetime,
        person: uuid.UUID,
        level: int,
        now: datetime,
    ) -> None:
        key = ESCALATED if level else REQUESTED
        category = "sla" if level else "approvals"
        urgent = bool(level)
        prefs = preferences_from_row(
            await session.scalar(
                select(NotificationPreference).where(NotificationPreference.user_id == person)
            )
        )
        path = approval_path(approval.id)
        inbox_url = absolute_url(self._settings.web_url, path)
        base = {
            "title": approval.title,
            "step": step_name,
            "due_at": due_at,
            "inbox_url": inbox_url,
            "expires_minutes": self._ttl // 60,
        }
        dedupe = f"approval:{approval.id}:{step_id}:{level}:{person}"
        urgency = Urgency.URGENT if urgent else Urgency.NORMAL
        recipient = Recipient.user(person)

        # Email.
        email_plan = plan(prefs, category=category, channel="email", urgent=urgent, now=now)
        email_vars = dict(base)
        if email_plan.action == "send":
            tokens = await self._links(session, approval, person, Channel.EMAIL_LINK)
            if tokens is not None:
                email_vars["approve_url"] = link_url(self._settings, tokens[0])
                email_vars["reject_url"] = link_url(self._settings, tokens[1])
        await notify(
            session,
            recipient=recipient,
            variables=email_vars,
            dedupe_key=dedupe,
            channels=["email"],
            extras=Extras(link=path),
            template_key=key,
            urgency=urgency,
            workflow=approval.workflow,
        )

        # Slack.
        integration = await active_slack(session)
        linked = (
            await slack_link_for_user(session, person, integration.external_id)
            if integration is not None
            else None
        )
        buttons: list[SlackButton] = []
        note: str | None = None
        slack_plan = plan(prefs, category=category, channel="slack", urgent=urgent, now=now)
        if linked is not None and slack_plan.action == "send":
            tokens = await self._links(session, approval, person, Channel.SLACK_LINK)
            if tokens is not None:
                buttons, note = approval_buttons(
                    locale=prefs.locale,
                    approve_url=link_url(self._settings, tokens[0]),
                    approve_token=tokens[0],
                    reject_url=link_url(self._settings, tokens[1]),
                    reject_token=tokens[1],
                )
        await notify(
            session,
            recipient=recipient,
            variables=base,
            dedupe_key=dedupe,
            channels=["slack"],
            extras=Extras(link=path, slack_buttons=buttons, slack_note=note),
            template_key=key,
            urgency=urgency,
            workflow=approval.workflow,
        )

        # In-app.
        await notify(
            session,
            recipient=recipient,
            variables=base,
            dedupe_key=dedupe,
            channels=["in_app"],
            extras=Extras(link=path),
            template_key=key,
            urgency=urgency,
            workflow=approval.workflow,
        )

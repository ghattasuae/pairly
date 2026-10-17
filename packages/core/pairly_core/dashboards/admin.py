# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The organisation admin dashboard (brief 5.1 item 13).

* **Action needed**: pending invitations, integrations that need setting up, the budget
  past its alert threshold, and what the workflows add (Hire: open data requests).
* **Users and roles**: members per role.
* **Integrations**: email (this deployment's transport) and Slack, plus each workflow's
  own (Hire: Google and Microsoft calendars, Zoom; the same checks as its settings page).
* **Approval rules**: every approval rule set with its latest version.
* **Policies**: rule sets and their versions, policy documents.
* **Templates**: notification templates and those that need approval to send.
* **Budget and usage**: model cost this month against the monthly budget.
* **Retention**: the rules and the latest dry-run report.
* **Data requests**: export and delete requests of every workflow that has them (Hire's
  candidate requests; Pay has none yet, so none is listed).

Each section needs the permission of the page it summarises; one the viewer lacks is left
out. Numbers with no data are ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.rules import RULES_KIND as APPROVAL_RULES_KIND
from pairly_core.dashboards.model import (
    Board,
    Dashboard,
    Item,
    Link,
    Section,
    SectionContext,
    SectionRegistry,
    Stat,
    Unit,
    may,
    plugin_sections,
    section,
)
from pairly_core.integrations.models import Integration
from pairly_core.llm.budget import ALERT_THRESHOLDS
from pairly_core.models import Invitation, Membership, ModelCall, OrgLLMBudget, PolicyVersion, Role
from pairly_core.notifications.models import Template
from pairly_core.policy.rules import RULES_KIND
from pairly_core.rbac import Actor
from pairly_core.retention.models import RetentionReport
from pairly_core.retention.rules import RetentionRegistry

MICROS: Final = 1_000_000


@dataclass(frozen=True, slots=True)
class AdminInputs:
    """What the admin board needs from the running process."""

    email_transport: str
    email_problems: Sequence[str]
    # None when the model settings could not be read (shown as no data).
    default_budget_micros: int | None
    retention: RetentionRegistry


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _members(session: AsyncSession) -> Section:
    rows = (
        await session.execute(
            select(Role.key, Membership.workflow, func.count())
            .join(Role, Role.id == Membership.role_id)
            .group_by(Role.key, Membership.workflow)
            .order_by(Role.key, Membership.workflow)
        )
    ).all()
    people = int(await session.scalar(select(func.count(func.distinct(Membership.user_id)))) or 0)
    return Section(
        key="members",
        count=people,
        href="/settings/members",
        items=[
            Item(
                kind="role",
                id=f"{role}:{workflow or 'all'}",
                label=role,
                href="/settings/members",
                detail={"workflow": workflow, "members": int(n)},
            )
            for role, workflow, n in rows
        ],
    )


async def _invitations(session: AsyncSession, now: datetime) -> Section:
    rows = (
        await session.execute(
            select(Invitation, Role.key)
            .join(Role, Role.id == Invitation.role_id)
            .where(
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > now,
            )
            .order_by(Invitation.expires_at)
        )
    ).all()
    return section(
        "pending_invitations",
        [
            Item(
                kind="invitation",
                id=str(inv.id),
                label=inv.email,
                href="/settings/members",
                at=inv.expires_at,
                detail={"role": role, "workflow": inv.workflow},
            )
            for inv, role in rows
        ],
        href="/settings/members",
    )


async def _integrations(session: AsyncSession, inputs: AdminInputs) -> Section:
    slack = await session.scalar(select(Integration).where(Integration.provider == "slack"))
    email_ok = not inputs.email_problems
    items = [
        Item(
            kind="integration",
            id="email",
            label="email",
            href="/settings/notifications",
            status="connected" if email_ok else "needs_setup",
            detail={"transport": inputs.email_transport, "problems": len(inputs.email_problems)},
        ),
        Item(
            kind="integration",
            id="slack",
            label="slack",
            href="/settings/notifications",
            status=(
                "connected"
                if slack is not None and slack.status == "active"
                else ("disabled" if slack is not None else "needs_setup")
            ),
            at=slack.updated_at if slack is not None else None,
            detail={"mapped_channels": len((slack.settings or {}).get("channel_mappings", {}))}
            if slack is not None
            else {},
        ),
    ]
    needing = sum(1 for i in items if i.status != "connected")
    return Section(
        key="integrations",
        count=needing,
        href="/settings/notifications",
        items=items,
        stats=[Stat("integrations_needing_setup", float(needing), href="/settings/notifications")],
    )


async def _latest_versions(session: AsyncSession, kind: str) -> list[PolicyVersion]:
    latest = (
        select(PolicyVersion.name, func.max(PolicyVersion.version).label("v"))
        .where(PolicyVersion.kind == kind)
        .group_by(PolicyVersion.name)
        .subquery()
    )
    return list(
        await session.scalars(
            select(PolicyVersion)
            .join(
                latest,
                (latest.c.name == PolicyVersion.name) & (latest.c.v == PolicyVersion.version),
            )
            .where(PolicyVersion.kind == kind)
            .order_by(PolicyVersion.name)
        )
    )


def _version_items(rows: Sequence[PolicyVersion], href: str) -> list[Item]:
    return [
        Item(
            kind="policy_version",
            id=str(p.id),
            label=p.name,
            href=href,
            at=p.created_at,
            detail={"version": p.version, "hash": p.content_hash[:12]},
        )
        for p in rows
    ]


async def _approval_rules(session: AsyncSession) -> Section:
    rows = await _latest_versions(session, APPROVAL_RULES_KIND)
    href = "/pay/approval-rules"
    return section("approval_rules", _version_items(rows, href), href=href)


async def _policies(session: AsyncSession) -> Section:
    rows = await _latest_versions(session, RULES_KIND)
    versions = int(
        await session.scalar(
            select(func.count()).select_from(PolicyVersion).where(PolicyVersion.kind == RULES_KIND)
        )
        or 0
    )
    out = section("policies", _version_items(rows, "/settings/policies"), href="/settings/policies")
    out.stats = [Stat("rule_set_versions", float(versions), href="/settings/policies")]
    return out


async def _templates(session: AsyncSession) -> Section:
    total = int(await session.scalar(select(func.count(func.distinct(Template.key)))) or 0)
    gated = int(
        await session.scalar(
            select(func.count(func.distinct(Template.key))).where(
                Template.requires_approval.is_(True)
            )
        )
        or 0
    )
    latest = list(
        await session.scalars(select(Template).order_by(Template.created_at.desc()).limit(10))
    )
    return Section(
        key="templates",
        count=total,
        href="/settings/notifications",
        items=[
            Item(
                kind="template",
                id=str(t.id),
                label=t.key,
                href="/settings/notifications",
                at=t.created_at,
                detail={
                    "version": t.version,
                    "locale": t.locale,
                    "channel": t.channel,
                    "requires_approval": t.requires_approval,
                },
            )
            for t in latest
        ],
        stats=[Stat("templates_requiring_approval", float(gated), href="/settings/notifications")],
    )


async def budget_section(
    session: AsyncSession, actor: Actor, inputs: AdminInputs, now: datetime
) -> tuple[Section, bool]:
    start = month_start(now)
    # A reused answer (``reused_from`` set) is a row with no model request and zero
    # cost: counted on its own so it is not read as spend.
    spent, calls, reused = (
        await session.execute(
            select(
                func.coalesce(func.sum(ModelCall.cost_micros), 0),
                func.count(),
                func.count().filter(ModelCall.reused_from.is_not(None)),
            ).where(ModelCall.created_at >= start)
        )
    ).one()
    custom = await session.scalar(
        select(OrgLLMBudget.monthly_limit_micros).where(OrgLLMBudget.org_id == actor.org_id)
    )
    limit = int(custom) if custom is not None else inputs.default_budget_micros
    ratio = (int(spent) / limit) if limit else None
    alert = ratio is not None and ratio * 100 >= min(ALERT_THRESHOLDS)
    budget = Section(
        key="budget",
        count=int(calls),
        stats=[
            Stat("spent_this_month", int(spent) / MICROS, Unit.USD, n=int(calls) - int(reused)),
            Stat("model_calls_this_month", float(calls)),
            Stat("reused_calls_this_month", float(reused), n=int(calls)),
            Stat(
                "monthly_budget",
                limit / MICROS if limit is not None else None,
                Unit.USD,
                text="custom" if custom is not None else "default",
            ),
            Stat("budget_used", ratio, Unit.RATIO),
        ],
        source="model_calls",
        note=None if calls else "no_data",
    )
    return budget, alert


async def _retention(session: AsyncSession, inputs: AdminInputs) -> Section:
    latest_run = await session.scalar(
        select(RetentionReport.run_id).order_by(RetentionReport.created_at.desc()).limit(1)
    )
    report: dict[str, int] = {}
    reported_at: datetime | None = None
    if latest_run is not None:
        for row in await session.scalars(
            select(RetentionReport).where(RetentionReport.run_id == latest_run)
        ):
            report[row.rule] = int(row.would_delete)
            reported_at = (
                row.created_at if reported_at is None else min(reported_at, row.created_at)
            )
    return Section(
        key="retention",
        count=len(inputs.retention.rules),
        items=[
            Item(
                kind="retention_rule",
                id=r.key,
                label=r.key,
                at=reported_at,
                detail={"days": r.days, "would_delete": report.get(r.key)},
            )
            for r in inputs.retention.rules
        ],
        source="dry_run_report" if latest_run is not None else None,
        note=None if latest_run is not None else "no_report_yet",
    )


async def admin_dashboard(
    session: AsyncSession,
    actor: Actor,
    *,
    now: datetime,
    sections: SectionRegistry,
    inputs: AdminInputs,
) -> Dashboard:
    action: list[Section] = []
    rest: list[Section] = []
    if may(actor, "core.users.read"):
        rest.append(await _members(session))
        invitations = await _invitations(session, now)
        action.append(invitations)
    integrations = await _integrations(session, inputs)
    rest.append(integrations)
    rest.append(await _approval_rules(session))
    if may(actor, "core.policy.read"):
        rest.append(await _policies(session))
    rest.append(await _templates(session))
    budget, alert = await budget_section(session, actor, inputs, now)
    rest.append(budget)
    if alert:
        action.append(
            Section(key="budget_alert", count=1, stats=budget.stats, note="over_threshold")
        )
    if may(actor, "core.retention.read"):
        rest.append(await _retention(session, inputs))
    extra_action, extra = await plugin_sections(
        sections, Board.ADMIN, SectionContext(session=session, actor=actor, now=now)
    )
    if integrations.count:
        action.append(
            Section(
                key="integrations_needing_setup",
                count=integrations.count,
                href=integrations.href,
                items=[i for i in integrations.items if i.status != "connected"],
            )
        )
    action.extend(extra_action)
    rest.extend(extra)
    links = [
        Link(key, href)
        for key, href, permission in (
            ("members", "/settings/members", "core.users.read"),
            ("policies", "/settings/policies", "core.policy.read"),
            ("flags", "/settings/flags", "core.flags.read"),
            ("webhooks", "/settings/webhooks", "core.webhooks.read"),
            ("api_keys", "/settings/api-keys", "core.api_keys.manage"),
        )
        if actor.has(permission)
    ]
    return Dashboard(board=Board.ADMIN, generated_at=now, action=action, sections=rest, links=links)

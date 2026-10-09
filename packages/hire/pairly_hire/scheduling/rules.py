# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The organisation's scheduling settings and each interviewer's availability rules.

Defaults when nothing is saved:

* organisation: time zone Asia/Dubai, 45 minute interviews, 30 minute steps, 12 hours
  notice, slots up to 14 days ahead, self-scheduling links valid 7 days;
* a person: the time zone of their notification preferences (or the organisation's),
  Monday to Friday 09:00 to 17:00, 15 minutes free after each interview, at most 4
  interviews a day, no holidays.

Every change is audited (``hire.scheduling.settings_changed``,
``hire.interviewer.availability_changed``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.errors import BadRequestError
from pairly_core.models import User
from pairly_core.notifications.models import NotificationPreference
from pairly_core.rbac import Actor
from pairly_core.scheduling.availability import (
    AvailabilityError,
    Rules,
    WorkingHours,
    valid_timezone,
)
from pairly_hire.scheduling.models import (
    DEFAULT_DURATION,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_LINK_DAYS,
    DEFAULT_NOTICE_HOURS,
    DEFAULT_SEED,
    DEFAULT_STEP,
    DEFAULT_TIMEZONE,
    HireInterviewerSettings,
    HireSchedulingSettings,
)

WORKFLOW: Final = "hire"
DEFAULT_BUFFER_AFTER: Final = 15
DEFAULT_MAX_PER_DAY: Final = 4


@dataclass(frozen=True, slots=True)
class OrgScheduling:
    timezone: str = DEFAULT_TIMEZONE
    duration_min: int = DEFAULT_DURATION
    step_min: int = DEFAULT_STEP
    notice_hours: int = DEFAULT_NOTICE_HOURS
    horizon_days: int = DEFAULT_HORIZON_DAYS
    link_days: int = DEFAULT_LINK_DAYS
    seed: str = DEFAULT_SEED

    @property
    def step(self) -> timedelta:
        return timedelta(minutes=self.step_min)

    @property
    def notice(self) -> timedelta:
        return timedelta(hours=self.notice_hours)


async def org_scheduling(session: AsyncSession) -> OrgScheduling:
    row = await session.scalar(select(HireSchedulingSettings))
    if row is None:
        return OrgScheduling()
    return OrgScheduling(
        timezone=row.default_timezone,
        duration_min=row.default_duration_min,
        step_min=row.slot_step_min,
        notice_hours=row.min_notice_hours,
        horizon_days=row.horizon_days,
        link_days=row.link_days,
        seed=row.fake_seed,
    )


_ORG_FIELDS: Final = {
    "timezone": "default_timezone",
    "duration_min": "default_duration_min",
    "step_min": "slot_step_min",
    "notice_hours": "min_notice_hours",
    "horizon_days": "horizon_days",
    "link_days": "link_days",
}


async def save_org_scheduling(
    session: AsyncSession, actor: Actor, values: Mapping[str, Any]
) -> OrgScheduling:
    """Change the organisation's settings (validated by the route schema; the time zone
    here). Audited with the changed fields."""
    if "timezone" in values:
        try:
            valid_timezone(str(values["timezone"]))
        except AvailabilityError:
            raise BadRequestError("unknown time zone") from None
    row = await session.scalar(select(HireSchedulingSettings).with_for_update())
    if row is None:
        row = HireSchedulingSettings(id=uuid.uuid4(), org_id=actor.org_id)
        session.add(row)
        await session.flush()
    changed: dict[str, Any] = {}
    for key, column in _ORG_FIELDS.items():
        if key in values and values[key] is not None and getattr(row, column) != values[key]:
            setattr(row, column, values[key])
            changed[key] = values[key]
    row.updated_by = actor.id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    if changed:
        await audit_actor_action(
            session,
            actor,
            "hire.scheduling.settings_changed",
            entity_type="hire.settings",
            entity_id=row.id,
            workflow=WORKFLOW,
            payload=changed,
        )
    return await org_scheduling(session)


# ------------------------------------------------------------------ interviewers


@dataclass(frozen=True, slots=True)
class Interviewer:
    id: uuid.UUID
    name: str
    email: str
    rules: Rules
    # Whether the person saved their own rules (else these are the defaults).
    saved: bool


def default_rules(tz: str) -> Rules:
    return Rules(
        timezone=tz,
        hours=WorkingHours.standard(),
        buffer_after=timedelta(minutes=DEFAULT_BUFFER_AFTER),
        max_per_day=DEFAULT_MAX_PER_DAY,
    )


def rules_from_row(row: HireInterviewerSettings) -> Rules:
    holidays: set[date] = set()
    for value in row.holidays or []:
        try:
            holidays.add(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return Rules(
        timezone=row.timezone,
        hours=WorkingHours.from_json(row.working_hours),
        buffer_before=timedelta(minutes=row.buffer_before_min),
        buffer_after=timedelta(minutes=row.buffer_after_min),
        max_per_day=row.max_per_day,
        holidays=frozenset(holidays),
    )


def display_name(user: User) -> str:
    return (user.display_name or user.email.split("@", 1)[0]).strip()


async def interviewers(
    session: AsyncSession, user_ids: Sequence[uuid.UUID], org: OrgScheduling
) -> dict[uuid.UUID, Interviewer]:
    """The people among ``user_ids`` with their rules (saved, or the defaults)."""
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    users = {u.id: u for u in await session.scalars(select(User).where(User.id.in_(ids)))}
    saved = {
        r.user_id: r
        for r in await session.scalars(
            select(HireInterviewerSettings).where(HireInterviewerSettings.user_id.in_(ids))
        )
    }
    found = await session.execute(
        select(NotificationPreference.user_id, NotificationPreference.timezone).where(
            NotificationPreference.user_id.in_(ids)
        )
    )
    zones: dict[uuid.UUID, str] = {row[0]: row[1] for row in found.all()}
    out: dict[uuid.UUID, Interviewer] = {}
    for user_id in ids:
        user = users.get(user_id)
        if user is None:
            continue
        row = saved.get(user_id)
        if row is not None:
            try:
                rules = rules_from_row(row)
            except AvailabilityError:
                rules = default_rules(org.timezone)
        else:
            tz = zones.get(user_id)
            try:
                tz = valid_timezone(tz) if tz and tz != "UTC" else org.timezone
            except AvailabilityError:
                tz = org.timezone
            rules = default_rules(tz)
        out[user_id] = Interviewer(user_id, display_name(user), user.email, rules, row is not None)
    return out


async def save_interviewer_rules(
    session: AsyncSession,
    actor: Actor,
    user_id: uuid.UUID,
    *,
    timezone: str,
    working_hours: Mapping[str, Any],
    buffer_before_min: int,
    buffer_after_min: int,
    max_per_day: int | None,
    holidays: Sequence[str],
) -> Rules:
    """Save a person's interview availability (validated as ``Rules``). Audited."""
    try:
        hours = WorkingHours.from_json(working_hours)
        days = sorted({date.fromisoformat(h) for h in holidays})
        rules = Rules(
            timezone=valid_timezone(timezone),
            hours=hours,
            buffer_before=timedelta(minutes=buffer_before_min),
            buffer_after=timedelta(minutes=buffer_after_min),
            max_per_day=max_per_day,
            holidays=frozenset(days),
        )
    except (AvailabilityError, ValueError) as exc:
        raise BadRequestError(f"invalid availability: {exc}") from None
    row = await session.scalar(
        select(HireInterviewerSettings)
        .where(HireInterviewerSettings.user_id == user_id)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HireInterviewerSettings(id=uuid.uuid4(), org_id=actor.org_id, user_id=user_id)
        session.add(row)
    row.timezone = rules.timezone
    row.working_hours = hours.to_json()
    row.buffer_before_min = buffer_before_min
    row.buffer_after_min = buffer_after_min
    row.max_per_day = max_per_day
    row.holidays = [d.isoformat() for d in days]
    row.updated_by = actor.id
    row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.interviewer.availability_changed",
        entity_type="user",
        entity_id=user_id,
        workflow=WORKFLOW,
        payload={
            "timezone": rules.timezone,
            "working_hours": row.working_hours,
            "buffer_before_min": buffer_before_min,
            "buffer_after_min": buffer_after_min,
            "max_per_day": max_per_day,
            "holidays": len(days),
        },
    )
    return rules

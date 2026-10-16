# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention with automatic deletion (brief 6.9), under each organisation's settings.

The daily job (``run_retention``) does, for every organisation, in its own tenant
sessions (RLS applies everywhere):

1. **Plan** (one transaction). For every rule: is it on, with which period
   (``settings``), and what is due. A subjects rule lists each due subject in the preview
   list (``retention_schedule``): first listed today, deletable seven days later
   (``delete_after``), or ``held`` while a legal hold stands. A subject no longer due (a
   candidate who applied again) leaves the list. A report row per rule says how many are
   due and when they are scheduled for deletion.
2. **Delete**, rule by rule, only for rules that are on:

   * ``rows``: the rows that were already due seven days ago (so every one of them was in
     a report for the whole preview), in one statement, audited
     (``core.retention.rows_deleted``) with a decision record (system actor).
   * ``subjects``: first the subjects whose files are still being removed (retried),
     then the listed subjects whose preview ended, oldest first, up to the organisation's
     daily cap (default 200). Each subject in its own transaction: its row of the list
     locked, the legal hold checked again, the subject checked still due, the plugin's
     erasure (the database part), a decision record (kind ``final_action``,
     ``retention.deleted``, system actor ``pairly.retention``) and the audit event
     ``core.retention.subject_erased``. Any error rolls all of it back and is recorded
     on the list row (fail closed: nothing half deleted). After that commit the stored
     files go (``finish``); until they are all gone the row stays ``erasing`` and the next
     run tries again. Then ``core.retention.subject_deleted``.

3. The report rows get what was deleted.

A rule that is off lists nothing and deletes nothing; turning it on starts a new
seven-day preview. A legal hold (``place_hold``) moves the subject's list rows to
``held`` in the same transaction; releasing it lists them again with a fresh preview.

**First deletion.** Before a subjects rule deletes anything for the first time in an
organisation, the plan sends its admins an urgent notice (``pairly_core.retention.
notice``: "Retention will start deleting N ... on <date>. Review the list or pause.")
and records it on ``retention_rule_controls``, once per organisation and rule. Every
listed subject of the rule is then kept at least a whole preview after the notice, so
the date it names holds. Until a notice is recorded (no admin could be told) the rule
deletes nothing.

**Pause.** An admin pauses a rule (``pause_rule``, audited): the list stays visible and
keeps being refreshed, but nothing of the rule is deleted (each deletion checks the
pause in its own transaction, so a pause stops a run already going). Subjects whose
database part was already erased still get their stored files removed. Resuming
(``resume_rule``) starts a new seven-day preview for every listed subject.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.rbac import Actor
from pairly_core.retention.models import (
    DEFAULT_DAILY_CAP,
    MAX_DAILY_CAP,
    PREVIEW_DAYS,
    RetentionHold,
    RetentionOrgSetting,
    RetentionReport,
    RetentionRuleControl,
    RetentionRuleSetting,
    RetentionScheduleItem,
    ScheduleStatus,
)
from pairly_core.retention.notice import RetentionNotifier, default_notifier
from pairly_core.retention.rules import (
    MAX_DAYS,
    DeleteMode,
    RetentionRegistry,
    RetentionRule,
)

log = structlog.get_logger("pairly_core.retention")

SYSTEM_ACTOR: Final = "pairly.retention"
DELETED_ACTION: Final = "retention.deleted"
SETTINGS_CHANGED: Final = "core.retention.settings_changed"
ROWS_DELETED: Final = "core.retention.rows_deleted"
SUBJECT_ERASED: Final = "core.retention.subject_erased"
SUBJECT_DELETED: Final = "core.retention.subject_deleted"
ERASE_FAILED: Final = "core.retention.erase_failed"
HOLD_PLACED: Final = "core.retention.hold_placed"
HOLD_RELEASED: Final = "core.retention.hold_released"
RULE_PAUSED: Final = "core.retention.rule_paused"
RULE_RESUMED: Final = "core.retention.rule_resumed"
FIRST_NOTICE: Final = "core.retention.first_notice_sent"
RULE_ENTITY: Final = "retention_rule"
HOLD_ENTITY: Final = "retention_hold"
MAX_REASON: Final = 2000
PREVIEW: Final = timedelta(days=PREVIEW_DAYS)


async def _now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    if not isinstance(value, datetime):  # pragma: no cover - now() always answers
        raise RuntimeError("the database returned no time")
    return value


def _org(session: AsyncSession) -> uuid.UUID:
    org = current_org(session)
    if org is None:
        raise TenantError("retention needs a tenant context")
    return org


# ------------------------------------------------------------------ settings


@dataclass(frozen=True, slots=True)
class RuleState:
    rule: RetentionRule
    enabled: bool
    # The period in days for ``rows`` and ``report`` rules and for subjects rules without
    # a plugin period; ``period_value`` in the plugin's unit otherwise.
    days: int
    customised: bool
    period_value: int | None = None
    paused_at: datetime | None = None
    notice_sent_at: datetime | None = None

    @property
    def paused(self) -> bool:
        return self.paused_at is not None


async def _controls(session: AsyncSession) -> dict[str, RetentionRuleControl]:
    return {c.rule: c for c in await session.scalars(select(RetentionRuleControl))}


async def rule_states(session: AsyncSession, registry: RetentionRegistry) -> list[RuleState]:
    saved = {s.rule: s for s in await session.scalars(select(RetentionRuleSetting))}
    controls = await _controls(session)
    states: list[RuleState] = []
    for rule in registry.rules:
        row = saved.get(rule.key)
        enabled = row.enabled if row is not None else rule.default_enabled
        days = row.days if row is not None and row.days is not None else rule.days
        if not rule.configurable_days:
            days = rule.days
        period_value = await rule.period.get(session) if rule.period is not None else None
        control = controls.get(rule.key)
        states.append(
            RuleState(
                rule=rule,
                enabled=enabled,
                days=days,
                customised=row is not None,
                period_value=period_value,
                paused_at=control.paused_at if control is not None else None,
                notice_sent_at=control.notice_sent_at if control is not None else None,
            )
        )
    return states


async def daily_cap(session: AsyncSession) -> int:
    value = await session.scalar(select(RetentionOrgSetting.daily_cap))
    return int(value) if value is not None else DEFAULT_DAILY_CAP


@dataclass(frozen=True, slots=True)
class RuleChange:
    rule: str
    enabled: bool | None = None
    days: int | None = None
    # A plugin period (``PeriodHook``), in its unit.
    period: int | None = None


async def update_settings(
    session: AsyncSession,
    actor: Actor,
    registry: RetentionRegistry,
    *,
    changes: Sequence[RuleChange] = (),
    cap: int | None = None,
) -> None:
    """Change rules (on or off, period) and the daily cap, audited with before and after.
    Turning a subjects rule off empties its preview list at once."""
    org = _org(session)
    now = await _now(session)
    before = {s.rule.key: s for s in await rule_states(session, registry)}
    audit_changes: list[dict[str, Any]] = []
    for change in changes:
        rule = registry.get(change.rule)
        state = before.get(change.rule)
        if rule is None or state is None:
            raise BadRequestError(f"unknown retention rule {change.rule!r}")
        if change.days is not None:
            if not rule.configurable_days or rule.period is not None:
                raise BadRequestError(f"the period of {rule.key!r} cannot be changed here")
            if not 0 <= change.days <= MAX_DAYS:
                raise BadRequestError(f"days must be 0 to {MAX_DAYS}")
        if change.period is not None:
            hook = rule.period
            if hook is None:
                raise BadRequestError(f"{rule.key!r} has no period of its own")
            if not hook.minimum <= change.period <= hook.maximum:
                raise BadRequestError(
                    f"the period must be {hook.minimum} to {hook.maximum} {hook.unit}"
                )
            await hook.set(session, change.period, actor)
        row = await session.scalar(
            select(RetentionRuleSetting)
            .where(RetentionRuleSetting.rule == rule.key)
            .with_for_update()
        )
        enabled = change.enabled if change.enabled is not None else state.enabled
        days = change.days if change.days is not None else (row.days if row else None)
        if row is None:
            session.add(
                RetentionRuleSetting(
                    org_id=org,
                    rule=rule.key,
                    enabled=enabled,
                    days=days,
                    updated_by=actor.id,
                    updated_at=now,
                )
            )
        else:
            row.enabled = enabled
            row.days = days
            row.updated_by = actor.id
            row.updated_at = now
        if not enabled and rule.mode is DeleteMode.SUBJECTS:
            await session.execute(
                delete(RetentionScheduleItem).where(
                    RetentionScheduleItem.rule == rule.key,
                    RetentionScheduleItem.status.in_(
                        [ScheduleStatus.SCHEDULED.value, ScheduleStatus.HELD.value]
                    ),
                )
            )
        audit_changes.append(
            {
                "rule": rule.key,
                "enabled": [state.enabled, enabled],
                "days": [state.days, days if days is not None else rule.days],
                **(
                    {"period": [state.period_value, change.period]}
                    if change.period is not None
                    else {}
                ),
            }
        )
    cap_change: list[int] | None = None
    if cap is not None:
        if not 1 <= cap <= MAX_DAILY_CAP:
            raise BadRequestError(f"the daily cap must be 1 to {MAX_DAILY_CAP}")
        current = await session.scalar(select(RetentionOrgSetting).with_for_update())
        cap_change = [current.daily_cap if current else DEFAULT_DAILY_CAP, cap]
        if current is None:
            session.add(
                RetentionOrgSetting(org_id=org, daily_cap=cap, updated_by=actor.id, updated_at=now)
            )
        else:
            current.daily_cap = cap
            current.updated_by = actor.id
            current.updated_at = now
    await session.flush()
    if audit_changes or cap_change is not None:
        await audit_actor_action(
            session,
            actor,
            SETTINGS_CHANGED,
            entity_type="retention_settings",
            entity_id=org,
            payload={"rules": audit_changes, "daily_cap": cap_change},
        )


# ------------------------------------------------------------------ pause


async def _control_for_update(session: AsyncSession, rule_key: str) -> RetentionRuleControl:
    """The rule's control row, locked (created when missing)."""
    org = _org(session)
    await session.execute(
        text(
            "INSERT INTO retention_rule_controls (org_id, rule) VALUES (:org, :rule) "
            "ON CONFLICT (org_id, rule) DO NOTHING"
        ),
        {"org": org, "rule": rule_key},
    )
    row = await session.scalar(
        select(RetentionRuleControl)
        .where(RetentionRuleControl.rule == rule_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None  # noqa: S101 (inserted above, same tenant)
    return row


def _pausable(registry: RetentionRegistry, rule_key: str) -> RetentionRule:
    rule = registry.get(rule_key)
    if rule is None:
        raise NotFoundError(f"unknown retention rule {rule_key!r}")
    if rule.mode is DeleteMode.REPORT:
        raise BadRequestError(f"{rule_key!r} only reports; there is nothing to pause")
    return rule


async def pause_rule(
    session: AsyncSession, actor: Actor, registry: RetentionRegistry, rule_key: str
) -> None:
    """Stop a rule deleting anything until a person resumes it. Its list stays."""
    rule = _pausable(registry, rule_key)
    row = await _control_for_update(session, rule.key)
    if row.paused_at is not None:
        raise ConflictError("this rule is already paused")
    now = await _now(session)
    row.paused_at = now
    row.paused_by = actor.id
    row.updated_at = now
    listed = await session.scalar(
        select(func.count()).where(
            RetentionScheduleItem.rule == rule.key,
            RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
        )
    )
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        RULE_PAUSED,
        entity_type=RULE_ENTITY,
        entity_id=rule.key,
        payload={"rule": rule.key, "listed": int(listed or 0)},
    )


async def resume_rule(
    session: AsyncSession, actor: Actor, registry: RetentionRegistry, rule_key: str
) -> None:
    """End a pause. Every listed subject of the rule gets a new seven-day preview."""
    rule = _pausable(registry, rule_key)
    row = await _control_for_update(session, rule.key)
    if row.paused_at is None:
        raise ConflictError("this rule is not paused")
    now = await _now(session)
    paused_at = row.paused_at
    row.paused_at = None
    row.paused_by = None
    row.updated_at = now
    relisted = await session.execute(
        update(RetentionScheduleItem)
        .where(
            RetentionScheduleItem.rule == rule.key,
            RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
        )
        .values(listed_at=now, delete_after=now + PREVIEW, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        RULE_RESUMED,
        entity_type=RULE_ENTITY,
        entity_id=rule.key,
        payload={
            "rule": rule.key,
            "paused_at": paused_at.isoformat(),
            "relisted": int(getattr(relisted, "rowcount", 0) or 0),
            "delete_after": (now + PREVIEW).isoformat(),
        },
    )


async def _paused(session: AsyncSession, rule_key: str) -> bool:
    """Whether the rule is paused, read under a share lock so a pause committing at the
    same time waits for this deletion's transaction or stops it."""
    paused_at = await session.scalar(
        select(RetentionRuleControl.paused_at)
        .where(RetentionRuleControl.rule == rule_key)
        .with_for_update(read=True)
    )
    return paused_at is not None


# ------------------------------------------------------------------ legal holds


def _clean_reason(value: str) -> str:
    cleaned = "".join(ch for ch in (value or "") if ch == "\n" or ch >= " ").strip()
    if not cleaned:
        raise BadRequestError("a reason is required")
    if len(cleaned) > MAX_REASON:
        raise BadRequestError(f"the reason is longer than {MAX_REASON} characters")
    return cleaned


async def place_hold(
    session: AsyncSession,
    actor: Actor,
    registry: RetentionRegistry,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    reason: str,
) -> RetentionHold:
    """A legal hold on one candidate or vendor: no rule deletes anything about it until
    a person releases it. Its rows of the preview list become ``held`` at once."""
    org = _org(session)
    if subject_type not in registry.hold_subjects:
        raise BadRequestError("a legal hold cannot name this kind of subject")
    text_value = _clean_reason(reason)
    existing = await session.scalar(
        select(RetentionHold.id).where(
            RetentionHold.subject_type == subject_type,
            RetentionHold.subject_id == subject_id,
            RetentionHold.released_at.is_(None),
        )
    )
    if existing is not None:
        raise ConflictError("a legal hold already stands for this subject")
    now = await _now(session)
    hold_id = uuid.uuid4()
    session.add(
        RetentionHold(
            id=hold_id,
            org_id=org,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=seal(RetentionHold, "reason", text_value, org_id=org, row_id=hold_id),
            placed_by=actor.id,
            placed_at=now,
        )
    )
    held = await session.execute(
        update(RetentionScheduleItem)
        .where(
            RetentionScheduleItem.subject_type == subject_type,
            RetentionScheduleItem.subject_id == subject_id,
            RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
        )
        .values(status=ScheduleStatus.HELD.value, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        HOLD_PLACED,
        entity_type=HOLD_ENTITY,
        entity_id=hold_id,
        payload={
            "subject_type": subject_type,
            "subject_id": str(subject_id),
            "listed_rows_held": int(getattr(held, "rowcount", 0) or 0),
        },
    )
    row = await session.scalar(select(RetentionHold).where(RetentionHold.id == hold_id))
    assert row is not None  # noqa: S101 (inserted above, same tenant)
    return row


async def release_hold(session: AsyncSession, actor: Actor, hold_id: uuid.UUID) -> RetentionHold:
    """End a legal hold. The subject's held rows of the list are listed again with a
    fresh seven-day preview (the next run checks they are still due)."""
    row = await session.scalar(
        select(RetentionHold).where(RetentionHold.id == hold_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("hold not found")
    if row.released_at is not None:
        raise ConflictError("this hold was already released")
    now = await _now(session)
    row.released_at = now
    row.released_by = actor.id
    await session.execute(
        update(RetentionScheduleItem)
        .where(
            RetentionScheduleItem.subject_type == row.subject_type,
            RetentionScheduleItem.subject_id == row.subject_id,
            RetentionScheduleItem.status == ScheduleStatus.HELD.value,
        )
        .values(
            status=ScheduleStatus.SCHEDULED.value,
            listed_at=now,
            delete_after=now + PREVIEW,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        HOLD_RELEASED,
        entity_type=HOLD_ENTITY,
        entity_id=row.id,
        payload={"subject_type": row.subject_type, "subject_id": str(row.subject_id)},
    )
    return row


async def held_subjects(session: AsyncSession, subject_type: str) -> set[uuid.UUID]:
    return set(
        await session.scalars(
            select(RetentionHold.subject_id).where(
                RetentionHold.subject_type == subject_type, RetentionHold.released_at.is_(None)
            )
        )
    )


# ------------------------------------------------------------------ plan


@dataclass(frozen=True, slots=True)
class RuleCount:
    rule: str
    cutoff: datetime
    would_delete: int
    enabled: bool = True
    scheduled_for: datetime | None = None
    held: int = 0


async def _plan_subjects(session: AsyncSession, state: RuleState, now: datetime) -> RuleCount:
    rule = state.rule
    subjects = rule.subjects
    assert subjects is not None  # noqa: S101 (mode SUBJECTS has its SubjectRule)
    due = {d.subject_id: d for d in await subjects.due(session, now, state.days)}
    existing = {
        item.subject_id: item
        for item in await session.scalars(
            select(RetentionScheduleItem)
            .where(RetentionScheduleItem.rule == rule.key)
            .with_for_update()
        )
    }
    if not state.enabled:
        # Off: nothing listed (a later switch on starts a new preview).
        await session.execute(
            delete(RetentionScheduleItem).where(
                RetentionScheduleItem.rule == rule.key,
                RetentionScheduleItem.status.in_(
                    [ScheduleStatus.SCHEDULED.value, ScheduleStatus.HELD.value]
                ),
            )
        )
        return RuleCount(rule.key, rule.cutoff(now, state.days), len(due), enabled=False)
    holds = await held_subjects(session, subjects.subject_type)
    org = _org(session)
    for subject_id, item in existing.items():
        if item.status not in (ScheduleStatus.SCHEDULED.value, ScheduleStatus.HELD.value):
            continue
        if subject_id not in due:
            await session.delete(item)  # no longer due (applied again, period changed)
            continue
        wanted = (
            ScheduleStatus.HELD.value if subject_id in holds else ScheduleStatus.SCHEDULED.value
        )
        if item.status != wanted:
            item.status = wanted
            if wanted == ScheduleStatus.SCHEDULED.value:
                item.listed_at, item.delete_after = now, now + PREVIEW
        item.due_at = due[subject_id].due_at
        item.label = due[subject_id].label
        item.updated_at = now
    for subject_id, d in due.items():
        if subject_id in existing:
            continue
        session.add(
            RetentionScheduleItem(
                org_id=org,
                rule=rule.key,
                subject_type=subjects.subject_type,
                subject_id=subject_id,
                label=d.label,
                due_at=d.due_at,
                listed_at=now,
                delete_after=now + PREVIEW,
                status=(
                    ScheduleStatus.HELD.value
                    if subject_id in holds
                    else ScheduleStatus.SCHEDULED.value
                ),
                updated_at=now,
            )
        )
    await session.flush()
    scheduled = list(
        await session.scalars(
            select(RetentionScheduleItem.delete_after).where(
                RetentionScheduleItem.rule == rule.key,
                RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
            )
        )
    )
    held = await session.scalar(
        select(func.count()).where(
            RetentionScheduleItem.rule == rule.key,
            RetentionScheduleItem.status == ScheduleStatus.HELD.value,
        )
    )
    return RuleCount(
        rule.key,
        rule.cutoff(now, state.days),
        len(scheduled),
        enabled=True,
        scheduled_for=min(scheduled) if scheduled else None,
        held=int(held or 0),
    )


async def _first_notice(
    session: AsyncSession,
    state: RuleState,
    notifier: RetentionNotifier,
    now: datetime,
) -> bool:
    """Before a subjects rule's first deletion: keep every listed subject a whole
    preview past now, tell the admins, record it. Rolled back (to a savepoint) and
    retried next run when no admin could be told. True when the notice is recorded."""
    rule = state.rule
    subjects = rule.subjects
    assert subjects is not None  # noqa: S101
    try:
        async with session.begin_nested():
            row = await _control_for_update(session, rule.key)
            if row.notice_sent_at is not None:
                return True
            await session.execute(
                update(RetentionScheduleItem)
                .where(
                    RetentionScheduleItem.rule == rule.key,
                    RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
                    RetentionScheduleItem.delete_after < now + PREVIEW,
                )
                .values(delete_after=now + PREVIEW, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            dates = list(
                await session.scalars(
                    select(RetentionScheduleItem.delete_after).where(
                        RetentionScheduleItem.rule == rule.key,
                        RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
                    )
                )
            )
            if not dates:
                return False
            deletes_on = min(dates)
            told = await notifier(
                session,
                rule=rule.key,
                workflow=rule.workflow,
                subject_type=subjects.subject_type,
                count=len(dates),
                deletes_on=deletes_on,
            )
            if not told:
                raise _NoAdminToldError
            row.notice_sent_at = now
            row.notice_count = len(dates)
            row.notice_deletes_on = deletes_on
            row.updated_at = now
            await session.flush()
            await append_audit(
                session,
                action=FIRST_NOTICE,
                actor_type=ActorType.SYSTEM,
                actor_id=SYSTEM_ACTOR,
                entity_type=RULE_ENTITY,
                entity_id=rule.key,
                workflow=rule.workflow,
                payload={
                    "rule": rule.key,
                    "count": len(dates),
                    "deletes_on": deletes_on.isoformat(),
                    "admins_told": told,
                },
            )
            return True
    except _NoAdminToldError:
        log.error("retention_notice_unsent", rule=rule.key)
        return False


class _NoAdminToldError(Exception):
    """No admin got the first-deletion notice (the savepoint rolls back)."""


async def plan_org(
    session: AsyncSession,
    registry: RetentionRegistry,
    *,
    run_id: uuid.UUID,
    now: datetime | None = None,
    notifier: RetentionNotifier | None = None,
) -> list[RuleCount]:
    """Count each rule's due rows or subjects, refresh the preview list and record a
    report row per rule (in the caller's transaction). Deletes nothing. With a
    ``notifier``, a subjects rule about to delete for the first time sends its notice."""
    org = _org(session)
    now = now or await _now(session)
    counts: list[RuleCount] = []
    for state in await rule_states(session, registry):
        rule = state.rule
        if rule.mode is DeleteMode.SUBJECTS:
            count = await _plan_subjects(session, state, now)
            if (
                notifier is not None
                and state.enabled
                and not state.paused
                and state.notice_sent_at is None
                and count.would_delete
            ):
                await _first_notice(session, state, notifier, now)
                count = dataclasses.replace(
                    count,
                    scheduled_for=await session.scalar(
                        select(func.min(RetentionScheduleItem.delete_after)).where(
                            RetentionScheduleItem.rule == rule.key,
                            RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
                        )
                    ),
                )
            if state.paused:
                count = dataclasses.replace(count, scheduled_for=None)
            counts.append(count)
            continue
        count = await session.scalar(
            select(func.count())
            .select_from(rule.model)
            # RLS limits the count to the organisation; the filter says so explicitly.
            .where(rule.model.org_id == org, *rule.due(now, state.days))
        )
        n = int(count or 0)
        scheduled_for = (
            now + PREVIEW
            if n and state.enabled and not state.paused and rule.mode is DeleteMode.ROWS
            else None
        )
        counts.append(
            RuleCount(
                rule.key,
                rule.cutoff(now, state.days),
                n,
                enabled=state.enabled,
                scheduled_for=scheduled_for,
            )
        )
    session.add_all(
        RetentionReport(
            org_id=org,
            run_id=run_id,
            rule=c.rule,
            cutoff=c.cutoff,
            would_delete=c.would_delete,
            scheduled_for=c.scheduled_for,
            enabled=c.enabled,
            deleted=0,
            dry_run=True,
        )
        for c in counts
    )
    await session.flush()
    return counts


async def dry_run_org(
    session: AsyncSession, registry: RetentionRegistry, *, run_id: uuid.UUID
) -> list[RuleCount]:
    """``plan_org`` (the name the earlier dry run had)."""
    return await plan_org(session, registry, run_id=run_id)


# ------------------------------------------------------------------ delete


async def _decision(
    session: AsyncSession,
    rule: RetentionRule,
    *,
    entity_type: str,
    entity_id: str,
    inputs: Mapping[str, Any],
    output: Mapping[str, Any],
) -> str:
    stored = await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.FINAL_ACTION,
            workflow=rule.workflow,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR,
            inputs=inputs,
            output=output,
            final_action=DELETED_ACTION,
        ),
    )
    return stored.id


async def delete_rows(session: AsyncSession, state: RuleState, now: datetime) -> int:
    """Delete the rows of a ``rows`` rule that were already due a whole preview ago, in
    the caller's transaction, audited with a decision record. Returns how many."""
    rule = state.rule
    org = _org(session)
    if await _paused(session, rule.key):
        return 0
    result = await session.execute(
        delete(rule.model)
        .where(rule.model.org_id == org, *rule.due(now - PREVIEW, state.days))
        .execution_options(synchronize_session=False)
    )
    n = int(getattr(result, "rowcount", 0) or 0)
    if not n:
        return 0
    inputs = {"rule": rule.key, "days": state.days, "preview_days": PREVIEW_DAYS}
    decision_id = await _decision(
        session,
        rule,
        entity_type=RULE_ENTITY,
        entity_id=rule.key,
        inputs=inputs,
        output={"deleted_rows": n},
    )
    await append_audit(
        session,
        action=ROWS_DELETED,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR,
        entity_type=RULE_ENTITY,
        entity_id=rule.key,
        workflow=rule.workflow,
        payload={**inputs, "deleted_rows": n, "decision_id": decision_id},
    )
    return n


@dataclass
class SubjectResult:
    erased: int = 0
    deleted: int = 0
    failed: int = 0
    held: int = 0


def _day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _finish(
    session: AsyncSession, state: RuleState, store: ObjectStore | None, item_id: uuid.UUID
) -> bool:
    """The stored files of one erased subject (its own transaction). True when done."""
    subjects = state.rule.subjects
    assert subjects is not None  # noqa: S101
    async with session.begin():
        item = await session.scalar(
            select(RetentionScheduleItem)
            .where(RetentionScheduleItem.id == item_id)
            .with_for_update()
        )
        if item is None or item.status != ScheduleStatus.ERASING.value:
            return False
        now = await _now(session)
        error: str | None = None
        try:
            if subjects.finish is not None:
                error = await subjects.finish(session, store, item.subject_id)
        except Exception as exc:
            error = f"finish_failed:{type(exc).__name__}"[:64]
        item.attempts += 1
        item.updated_at = now
        if error is not None:
            item.error = error
            return False
        item.status = ScheduleStatus.DELETED.value
        item.error = None
        item.deleted_at = now
        await session.flush()
        await append_audit(
            session,
            action=SUBJECT_DELETED,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR,
            entity_type=item.subject_type,
            entity_id=item.subject_id,
            workflow=state.rule.workflow,
            payload={"rule": state.rule.key, "decision_id": item.decision_id},
        )
        return True


async def _erase_one(
    session: AsyncSession, state: RuleState, item_id: uuid.UUID, due: set[uuid.UUID]
) -> str:
    """The database part of one subject, all or nothing (its own transaction). Returns
    ``erased``, ``held``, ``paused``, ``skipped`` or raises (rolled back)."""
    rule = state.rule
    subjects = rule.subjects
    assert subjects is not None  # noqa: S101
    async with session.begin():
        item = await session.scalar(
            select(RetentionScheduleItem)
            .where(RetentionScheduleItem.id == item_id)
            .with_for_update()
        )
        now = await _now(session)
        if item is None or item.status != ScheduleStatus.SCHEDULED.value or item.delete_after > now:
            return "skipped"
        if await _paused(session, rule.key):
            return "paused"
        if item.subject_id in await held_subjects(session, item.subject_type):
            item.status = ScheduleStatus.HELD.value
            item.updated_at = now
            return "held"
        if item.subject_id not in due:
            await session.delete(item)
            return "skipped"
        counts = dict(await subjects.erase(session, item.subject_id, SYSTEM_ACTOR))
        inputs = {
            "rule": rule.key,
            "days": state.days,
            **({"period": state.period_value} if state.period_value is not None else {}),
            "due_at": item.due_at.isoformat(),
            "listed_at": item.listed_at.isoformat(),
            "delete_after": item.delete_after.isoformat(),
        }
        decision_id = await _decision(
            session,
            rule,
            entity_type=item.subject_type,
            entity_id=str(item.subject_id),
            inputs=inputs,
            output={"erased": True, "cleared": counts},
        )
        item.status = ScheduleStatus.ERASING.value
        item.decision_id = decision_id
        item.erased_at = now
        item.error = None
        item.updated_at = now
        await session.flush()
        await append_audit(
            session,
            action=SUBJECT_ERASED,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR,
            entity_type=item.subject_type,
            entity_id=item.subject_id,
            workflow=rule.workflow,
            payload={"rule": rule.key, "decision_id": decision_id, **counts},
        )
        return "erased"


async def _record_failure(
    session: AsyncSession, state: RuleState, item_id: uuid.UUID, error: str
) -> None:
    async with session.begin():
        item = await session.scalar(
            select(RetentionScheduleItem)
            .where(RetentionScheduleItem.id == item_id)
            .with_for_update()
        )
        if item is None:
            return
        item.attempts += 1
        item.error = error[:64]
        item.updated_at = await _now(session)
        await session.flush()
        await append_audit(
            session,
            action=ERASE_FAILED,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR,
            entity_type=item.subject_type,
            entity_id=item.subject_id,
            workflow=state.rule.workflow,
            payload={"rule": state.rule.key, "error": item.error, "attempts": item.attempts},
        )


async def delete_subjects(
    session: AsyncSession,
    state: RuleState,
    store: ObjectStore | None,
    *,
    budget: int,
) -> tuple[SubjectResult, int]:
    """Finish the subjects still ``erasing``, then erase up to ``budget`` listed subjects
    whose preview ended. ``session`` is a tenant session outside a transaction; every
    step runs in its own. Returns what happened and the budget left."""
    result = SubjectResult()
    rule = state.rule
    subjects = rule.subjects
    assert subjects is not None  # noqa: S101
    async with session.begin():
        erasing = list(
            await session.scalars(
                select(RetentionScheduleItem.id).where(
                    RetentionScheduleItem.rule == rule.key,
                    RetentionScheduleItem.status == ScheduleStatus.ERASING.value,
                )
            )
        )
    for item_id in erasing:
        if await _finish(session, state, store, item_id):
            result.deleted += 1
    if budget <= 0:
        return result, budget
    async with session.begin():
        now = await _now(session)
        ready = list(
            await session.scalars(
                select(RetentionScheduleItem.id)
                .where(
                    RetentionScheduleItem.rule == rule.key,
                    RetentionScheduleItem.status == ScheduleStatus.SCHEDULED.value,
                    RetentionScheduleItem.delete_after <= now,
                )
                .order_by(RetentionScheduleItem.delete_after, RetentionScheduleItem.id)
                .limit(budget)
            )
        )
        due = (
            {d.subject_id for d in await subjects.due(session, now, state.days)} if ready else set()
        )
    for item_id in ready:
        if budget <= 0:
            break
        try:
            outcome = await _erase_one(session, state, item_id, due)
        except Exception as exc:
            # Rolled back: nothing of this subject changed. Recorded and retried next run.
            log.warning("retention_erase_failed", rule=rule.key, error=type(exc).__name__)
            budget -= 1
            result.failed += 1
            await _record_failure(session, state, item_id, f"erase_failed:{type(exc).__name__}")
            continue
        if outcome == "paused":
            break
        if outcome == "held":
            result.held += 1
            continue
        if outcome != "erased":
            continue
        budget -= 1
        result.erased += 1
        if await _finish(session, state, store, item_id):
            result.deleted += 1
    return result, budget


async def erased_today(session: AsyncSession, now: datetime) -> int:
    count = await session.scalar(
        select(func.count()).where(RetentionScheduleItem.erased_at >= _day_start(now))
    )
    return int(count or 0)


@dataclass
class RunSummary:
    run_id: uuid.UUID
    orgs: int = 0
    errors: int = 0
    totals: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "dry_run": self.dry_run,
            "orgs": self.orgs,
            "errors": self.errors,
            "would_delete": dict(sorted(self.totals.items())),
            "deleted": dict(sorted(self.deleted.items())),
            "failed": self.failed,
        }


async def run_org(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RetentionRegistry,
    org_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    store: ObjectStore | None,
    delete_data: bool = True,
    notifier: RetentionNotifier | None = None,
) -> tuple[list[RuleCount], dict[str, int], int]:
    """Plan, then (``delete_data``) delete for one organisation. Returns the counts, what
    each rule deleted and how many subjects failed. A paused rule deletes nothing; a
    subjects rule whose first-deletion notice is not recorded erases no one (it only
    finishes the stored files of subjects already erased)."""
    deleted: dict[str, int] = {}
    failed = 0
    session = sessionmaker()
    set_tenant(session, org_id, None)
    if delete_data and notifier is None:
        notifier = default_notifier()
    async with session:
        async with session.begin():
            counts = await plan_org(
                session, registry, run_id=run_id, notifier=notifier if delete_data else None
            )
            states = await rule_states(session, registry)
            now = await _now(session)
            budget = await daily_cap(session) - await erased_today(session, now)
        if delete_data:
            for state in states:
                if not state.enabled or state.rule.mode is DeleteMode.REPORT:
                    continue
                try:
                    if state.rule.mode is DeleteMode.ROWS:
                        if state.paused:
                            continue
                        async with session.begin():
                            n = await delete_rows(session, state, await _now(session))
                    else:
                        may_erase = not state.paused and state.notice_sent_at is not None
                        res, left = await delete_subjects(
                            session, state, store, budget=max(budget, 0) if may_erase else 0
                        )
                        if may_erase:
                            budget = left
                        n = res.erased
                        failed += res.failed
                except Exception as exc:
                    log.error(
                        "retention_rule_failed",
                        org_id=str(org_id),
                        rule=state.rule.key,
                        error=type(exc).__name__,
                    )
                    failed += 1
                    continue
                deleted[state.rule.key] = n
            if deleted:
                async with session.begin():
                    for rule_key, n in deleted.items():
                        await session.execute(
                            update(RetentionReport)
                            .where(
                                RetentionReport.run_id == run_id, RetentionReport.rule == rule_key
                            )
                            .values(deleted=n)
                            .execution_options(synchronize_session=False)
                        )
    return counts, deleted, failed


async def retention_org_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM retention_org_ids() AS org_id"))
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def run_retention(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RetentionRegistry,
    *,
    store: ObjectStore | None = None,
    run_id: uuid.UUID | None = None,
    delete_data: bool = True,
    notifier: RetentionNotifier | None = None,
) -> RunSummary:
    """The daily job over every organisation; one failing organisation never stops the
    others."""
    summary = RunSummary(run_id=run_id or uuid.uuid4(), dry_run=not delete_data)
    for org_id in await retention_org_ids(sessionmaker):
        summary.orgs += 1
        try:
            counts, deleted, failed = await run_org(
                sessionmaker,
                registry,
                org_id,
                run_id=summary.run_id,
                store=store,
                delete_data=delete_data,
                notifier=notifier,
            )
        except Exception as exc:
            summary.errors += 1
            log.error("retention_run_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        summary.failed += failed
        for c in counts:
            summary.totals[c.rule] = summary.totals.get(c.rule, 0) + c.would_delete
        for rule_key, n in deleted.items():
            summary.deleted[rule_key] = summary.deleted.get(rule_key, 0) + n
    return summary


async def run_dry_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RetentionRegistry,
    *,
    run_id: uuid.UUID | None = None,
) -> RunSummary:
    """Plan only, for every organisation: lists and reports, deletes nothing."""
    return await run_retention(sessionmaker, registry, run_id=run_id, delete_data=False)


# ------------------------------------------------------------------ read


async def latest_report(session: AsyncSession) -> list[RetentionReport]:
    """The rows of the organisation's most recent run (empty before the first)."""
    latest = await session.scalar(
        select(RetentionReport.run_id)
        .order_by(RetentionReport.created_at.desc(), RetentionReport.id)
        .limit(1)
    )
    if latest is None:
        return []
    return list(
        await session.scalars(
            select(RetentionReport)
            .where(RetentionReport.run_id == latest)
            .order_by(RetentionReport.rule)
        )
    )


async def schedule(
    session: AsyncSession, *, include_done: bool = False, limit: int = 500
) -> list[RetentionScheduleItem]:
    """The preview list: what is scheduled, held or being erased (and, with
    ``include_done``, what was deleted), soonest first."""
    statuses = [
        ScheduleStatus.SCHEDULED.value,
        ScheduleStatus.HELD.value,
        ScheduleStatus.ERASING.value,
    ]
    if include_done:
        statuses.append(ScheduleStatus.DELETED.value)
    return list(
        await session.scalars(
            select(RetentionScheduleItem)
            .where(RetentionScheduleItem.status.in_(statuses))
            .order_by(RetentionScheduleItem.delete_after, RetentionScheduleItem.id)
            .limit(max(1, min(limit, 2000)))
        )
    )


async def holds(session: AsyncSession) -> list[RetentionHold]:
    return list(
        await session.scalars(
            select(RetentionHold)
            .where(RetentionHold.released_at.is_(None))
            .order_by(RetentionHold.placed_at.desc())
        )
    )

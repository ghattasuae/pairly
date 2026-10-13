# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention of rejected candidates (brief 6.9). Default 12 months, set per organisation.

The engine's retention rules have one age per rule. To make the period configurable
per organisation without changing the engine, a rejected application stores the moment
it becomes due, ``retention_until = rejected_at + the organisation's months``
(``hire_org_settings.rejected_retention_months``, default 12), and the rule
``hire.applications.rejected`` counts rows whose ``retention_until`` has passed
(``days = 0``). Changing the setting recomputes ``retention_until`` of every rejected
application in the organisation (``apply_retention_months``).

Like every rule today this is a dry run: the engine counts and reports, nothing is
deleted (``pairly_core.retention.service``).

``hire.checklist.documents``: pre-employment checklist files (ID, degree, visa) past
their short retention, ``retention_until`` = hired at + the job's days (default 90,
``pairly_hire.onboarding``). The engine counts them like the others; the
``hire_checklist_retention`` job sets the dates and removes the files the hiring team's
own setting says to remove (encrypted objects first, then the row's pointer, audited).
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Final

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.retention.rules import RetentionRule
from pairly_hire.models import ApplicationStatus, HireApplication
from pairly_hire.onboarding.models import HireChecklistItem

RULE_KEY: Final = "hire.applications.rejected"
CHECKLIST_RULE_KEY: Final = "hire.checklist.documents"


def retention_until(rejected_at: datetime, months: int) -> datetime:
    """``rejected_at`` plus ``months`` calendar months (the day is clamped to the month)."""
    total = rejected_at.month - 1 + months
    year, month = rejected_at.year + total // 12, total % 12 + 1
    day = min(rejected_at.day, calendar.monthrange(year, month)[1])
    return rejected_at.replace(year=year, month=month, day=day)


RETENTION_RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule(
        RULE_KEY,
        "Rejected applications past the organisation's retention period (default 12 months)",
        HireApplication,
        "retention_until",
        0,
        lambda: (HireApplication.status == ApplicationStatus.REJECTED.value,),
    ),
    RetentionRule(
        CHECKLIST_RULE_KEY,
        "Pre-employment checklist files past their retention after hire (default 90 days)",
        HireChecklistItem,
        "retention_until",
        0,
        lambda: (HireChecklistItem.purged_at.is_(None), HireChecklistItem.object_key.is_not(None)),
    ),
)


async def apply_retention_months(session: AsyncSession, months: int) -> int:
    """Recompute ``retention_until`` of the organisation's rejected applications."""
    result = await session.execute(
        update(HireApplication)
        .where(
            HireApplication.status == ApplicationStatus.REJECTED.value,
            HireApplication.rejected_at.is_not(None),
        )
        .values(retention_until=HireApplication.rejected_at + func.make_interval(0, int(months)))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)

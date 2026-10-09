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
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Final

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.retention.rules import RetentionRule
from pairly_hire.models import ApplicationStatus, HireApplication

RULE_KEY: Final = "hire.applications.rejected"


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

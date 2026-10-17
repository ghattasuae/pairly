# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""When a rejected application becomes due for deletion (``pairly_hire.retention``).

Kept apart from the retention rule so the pipeline can set the date without importing
the erasure (which reads self-identification to delete it).
"""

from __future__ import annotations

import calendar
from datetime import datetime


def retention_until(rejected_at: datetime, months: int) -> datetime:
    """``rejected_at`` plus ``months`` calendar months (the day is clamped to the month)."""
    total = rejected_at.month - 1 + months
    year, month = rejected_at.year + total // 12, total % 12 + 1
    day = min(rejected_at.day, calendar.monthrange(year, month)[1])
    return rejected_at.replace(year=year, month=month, day=day)

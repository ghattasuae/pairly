# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A requisition's start date is checked against today in the organisation's zone."""

from __future__ import annotations

from datetime import UTC, date, datetime

from pairly_hire.org_time import local_today


def test_local_today_follows_the_organisation_zone_across_midnight() -> None:
    # 20:03 UTC on 1 March is 00:03 on 2 March in Dubai (UTC+4).
    late_utc = datetime(2026, 3, 1, 20, 3, tzinfo=UTC)
    assert local_today(late_utc, "Asia/Dubai") == date(2026, 3, 2)
    assert local_today(late_utc, "UTC") == date(2026, 3, 1)
    # One minute before Dubai midnight, Dubai is still on 1 March.
    assert local_today(datetime(2026, 3, 1, 19, 59, tzinfo=UTC), "Asia/Dubai") == date(2026, 3, 1)
    # A zone behind UTC: 02:00 UTC on 2 March is still 1 March in New York.
    early_utc = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    assert local_today(early_utc, "America/New_York") == date(2026, 3, 1)

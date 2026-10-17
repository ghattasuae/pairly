# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Preferences, digest and urgency matrix (``plan``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pairly_core.notifications.preferences import (
    DEFAULT_PREFERENCES,
    PreferenceError,
    Preferences,
    parse_preferences,
    plan,
    quiet_until,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # 16:00 in Dubai


def _prefs(**kw: object) -> Preferences:
    base: dict[str, object] = {
        "locale": "en",
        "timezone": "UTC",
        "channels": {},
        "digest": {},
        "snooze_until": None,
        "quiet_start": None,
        "quiet_end": None,
    }
    base.update(kw)
    return parse_preferences(**base)  # type: ignore[arg-type]


DIGEST = _prefs(digest={"approvals": "daily"})
SNOOZED = _prefs(snooze_until=NOW + timedelta(hours=2))
QUIET = _prefs(timezone="Asia/Dubai", quiet_start=15 * 60, quiet_end=8 * 60)  # wraps midnight
NO_EMAIL = _prefs(channels={"approvals": ["in_app", "slack"]})


@pytest.mark.parametrize(
    ("prefs", "channel", "urgent", "action", "reason"),
    [
        # Defaults: real time on every channel.
        (DEFAULT_PREFERENCES, "email", False, "send", "realtime"),
        (DEFAULT_PREFERENCES, "slack", False, "send", "realtime"),
        (DEFAULT_PREFERENCES, "in_app", False, "send", "in_app"),
        # Digest holds non-urgent email and Slack, never in-app, never urgent items.
        (DIGEST, "email", False, "digest", "digest"),
        (DIGEST, "slack", False, "digest", "digest"),
        (DIGEST, "in_app", False, "send", "in_app"),
        (DIGEST, "email", True, "send", "urgent"),
        # Snooze and quiet hours defer non-urgent items; urgent ones go now.
        (SNOOZED, "email", False, "defer", "snoozed"),
        (SNOOZED, "email", True, "send", "urgent"),
        (QUIET, "email", False, "defer", "quiet_hours"),
        (QUIET, "slack", True, "send", "urgent"),
        # Opting out of a channel wins over urgency.
        (NO_EMAIL, "email", False, "suppress", "opted_out"),
        (NO_EMAIL, "email", True, "suppress", "opted_out"),
        (NO_EMAIL, "slack", True, "send", "urgent"),
    ],
)
def test_plan_matrix(
    prefs: Preferences, channel: str, urgent: bool, action: str, reason: str
) -> None:
    decided = plan(prefs, category="approvals", channel=channel, urgent=urgent, now=NOW)
    assert (decided.action, decided.reason) == (action, reason)


def test_always_urgent_categories_bypass_digest_but_not_opt_out() -> None:
    prefs = _prefs(digest={"updates": "daily"})
    assert plan(prefs, category="sla", channel="email", urgent=False, now=NOW).action == "send"
    off = _prefs(channels={"sla": ["in_app"]})
    assert plan(off, category="sla", channel="email", urgent=False, now=NOW).action == "suppress"


def test_category_defaults() -> None:
    # "updates" goes to the digest by default; reminders never by email.
    assert (
        plan(DEFAULT_PREFERENCES, category="updates", channel="email", urgent=False, now=NOW).action
        == "digest"
    )
    assert (
        plan(
            DEFAULT_PREFERENCES, category="reminders", channel="email", urgent=False, now=NOW
        ).action
        == "suppress"
    )


def test_deferral_times() -> None:
    snoozed = plan(SNOOZED, category="approvals", channel="email", urgent=False, now=NOW)
    assert snoozed.deliver_at == NOW + timedelta(hours=2)
    quiet = quiet_until(QUIET, NOW)
    assert quiet is not None
    # 08:00 next morning in Dubai is 04:00 UTC.
    assert quiet.astimezone(UTC) == datetime(2026, 10, 8, 4, 0, tzinfo=UTC)
    assert quiet_until(QUIET, datetime(2026, 10, 7, 6, 0, tzinfo=UTC)) is None  # 10:00 Dubai


@pytest.mark.parametrize(
    "kw",
    [
        {"locale": "fr"},
        {"timezone": "Mars/Base"},
        {"channels": {"approvals": ["fax"]}},
        {"channels": {"receipts": ["email"]}},
        {"digest": {"approvals": "weekly"}},
        {"quiet_start": 60},
        {"quiet_start": 60, "quiet_end": 60},
        {"quiet_start": 60, "quiet_end": 5000},
        {"snooze_until": datetime(2026, 10, 7, 12, 0)},
    ],
)
def test_invalid_preferences(kw: dict[str, object]) -> None:
    with pytest.raises(PreferenceError):
        _prefs(**kw)

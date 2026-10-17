# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Notification categories and per-user preferences (brief 5.3, Notifications UX).

Each template belongs to a category. A person chooses, per category, which channels
they get (email, Slack, in-app) and whether email and Slack arrive in real time or in a
daily digest; they can snooze everything until a time, and set quiet hours in their own
time zone.

``plan`` decides what happens to one message on one channel, in this order:

1. The channel is turned off for the category: suppressed. This holds for urgent
   messages too: urgency never overrides an opt-out of a channel.
2. In-app: delivered (it is the feed; it never pings, so it is never held).
3. Urgent (the caller says so, or the category is always urgent, like SLA breaches):
   sent now. Urgent messages skip the digest, snooze and quiet hours, because they are
   the only things that should interrupt (SLA breaches, interviews starting soon,
   approvals blocking others).
4. The category is on the daily digest: held for the digest.
5. Snoozed: deferred until the snooze ends.
6. Inside quiet hours: deferred until they end.
7. Otherwise sent now.

External recipients (candidates, vendors, email senders) have no preferences; their
messages are transactional and always ``send``, except that the receipt rules in
``pairly_core.notifications.receipts`` and the unsubscribe list apply.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pairly_core.notifications.templating import CHANNELS, LOCALES

Delivery = Literal["realtime", "daily"]
Action = Literal["send", "digest", "defer", "suppress"]


@dataclass(frozen=True, slots=True)
class CategoryPolicy:
    # Transactional mail (receipts, approvals, messages about a person's own case) has no
    # List-Unsubscribe header; everything else does.
    transactional: bool
    default_channels: frozenset[str]
    default_delivery: Delivery = "realtime"
    always_urgent: bool = False
    # Whether staff can change it in their preferences.
    configurable: bool = True


_ALL = frozenset(CHANNELS)
CATEGORIES: Final[Mapping[str, CategoryPolicy]] = {
    "approvals": CategoryPolicy(True, _ALL),
    "sla": CategoryPolicy(True, _ALL, always_urgent=True),
    "reminders": CategoryPolicy(False, frozenset({"slack", "in_app"})),
    "interviews": CategoryPolicy(True, _ALL),
    "updates": CategoryPolicy(False, frozenset({"email", "in_app"}), "daily"),
    "digest": CategoryPolicy(False, frozenset({"email"})),
    "receipts": CategoryPolicy(True, frozenset({"email"}), configurable=False),
    "candidate_comms": CategoryPolicy(True, frozenset({"email"}), configurable=False),
    "vendor_comms": CategoryPolicy(True, frozenset({"email"}), configurable=False),
    # A person's request about their own data (export, deletion, human review): admins
    # must act within a legal deadline, so always urgent and never switched off.
    "data_requests": CategoryPolicy(
        True, frozenset({"email", "in_app"}), always_urgent=True, configurable=False
    ),
    # Retention is about to delete records for the first time: the admins hear of it a
    # whole preview ahead, at once, and cannot switch it off.
    "retention": CategoryPolicy(
        True, frozenset({"email", "in_app"}), always_urgent=True, configurable=False
    ),
    # Alerts about money at risk (a bank-detail change an invoice check opened): every
    # channel, at once, and never switched off.
    "urgent": CategoryPolicy(True, _ALL, always_urgent=True, configurable=False),
}
DEFAULT_POLICY: Final = CategoryPolicy(False, frozenset({"email", "in_app"}))


def category_policy(category: str) -> CategoryPolicy:
    return CATEGORIES.get(category, DEFAULT_POLICY)


@dataclass(frozen=True, slots=True)
class Preferences:
    locale: str = "en"
    timezone: str = "UTC"
    channels: Mapping[str, frozenset[str]] = field(default_factory=dict)
    digest: Mapping[str, Delivery] = field(default_factory=dict)
    snooze_until: datetime | None = None
    quiet_start: int | None = None  # minutes after local midnight
    quiet_end: int | None = None

    def enabled(self, category: str) -> frozenset[str]:
        if category in self.channels:
            return self.channels[category]
        return category_policy(category).default_channels

    def delivery(self, category: str) -> Delivery:
        return self.digest.get(category, category_policy(category).default_delivery)

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


DEFAULT_PREFERENCES: Final = Preferences()


class PreferenceError(ValueError):
    """Invalid preferences input."""


def validate_timezone(name: str) -> str:
    if not isinstance(name, str) or len(name) > 64:
        raise PreferenceError("invalid time zone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise PreferenceError("unknown time zone") from None
    return name


def parse_preferences(
    *,
    locale: str,
    timezone: str,
    channels: Mapping[str, Iterable[str]],
    digest: Mapping[str, str],
    snooze_until: datetime | None,
    quiet_start: int | None,
    quiet_end: int | None,
) -> Preferences:
    """Validate preferences from the API. Unknown categories and channels are refused."""
    if locale not in LOCALES:
        raise PreferenceError("unsupported locale")
    clean_channels: dict[str, frozenset[str]] = {}
    for category, values in channels.items():
        if category not in CATEGORIES or not CATEGORIES[category].configurable:
            raise PreferenceError(f"unknown or fixed category {category!r}")
        chosen = frozenset(values)
        if not chosen <= _ALL:
            raise PreferenceError("unknown channel")
        clean_channels[category] = chosen
    clean_digest: dict[str, Delivery] = {}
    for category, value in digest.items():
        if category not in CATEGORIES or not CATEGORIES[category].configurable:
            raise PreferenceError(f"unknown or fixed category {category!r}")
        if value not in ("realtime", "daily"):
            raise PreferenceError("delivery is realtime or daily")
        clean_digest[category] = "daily" if value == "daily" else "realtime"
    if (quiet_start is None) != (quiet_end is None):
        raise PreferenceError("quiet hours need a start and an end")
    if (
        quiet_start is not None
        and quiet_end is not None
        and (not (0 <= quiet_start < 1440 and 0 <= quiet_end < 1440) or quiet_start == quiet_end)
    ):
        raise PreferenceError("quiet hours are minutes after midnight, start and end differ")
    if snooze_until is not None and snooze_until.tzinfo is None:
        raise PreferenceError("snooze_until needs a time zone")
    return Preferences(
        locale=locale,
        timezone=validate_timezone(timezone),
        channels=clean_channels,
        digest=clean_digest,
        snooze_until=snooze_until,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
    )


def preferences_from_row(row: Any | None) -> Preferences:
    """``Preferences`` from a ``notification_preferences`` row (or the defaults)."""
    if row is None:
        return DEFAULT_PREFERENCES
    channels = {
        str(k): frozenset(str(c) for c in v if c in _ALL)
        for k, v in (row.channels or {}).items()
        if isinstance(v, list)
    }
    digest: dict[str, Delivery] = {
        str(k): ("daily" if v == "daily" else "realtime") for k, v in (row.digest or {}).items()
    }
    return Preferences(
        locale=row.locale if row.locale in LOCALES else "en",
        timezone=row.timezone,
        channels=channels,
        digest=digest,
        snooze_until=row.snooze_until,
        quiet_start=row.quiet_start,
        quiet_end=row.quiet_end,
    )


def _in_quiet(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def quiet_until(prefs: Preferences, now: datetime) -> datetime | None:
    """The end of the current quiet period, or None when not inside quiet hours."""
    if prefs.quiet_start is None or prefs.quiet_end is None:
        return None
    local = now.astimezone(prefs.zone())
    minute = local.hour * 60 + local.minute
    if not _in_quiet(minute, prefs.quiet_start, prefs.quiet_end):
        return None
    end = local.replace(
        hour=prefs.quiet_end // 60, minute=prefs.quiet_end % 60, second=0, microsecond=0
    )
    if end <= local:
        end += timedelta(days=1)
    return end


@dataclass(frozen=True, slots=True)
class Plan:
    action: Action
    reason: str
    deliver_at: datetime | None = None


def plan(
    prefs: Preferences,
    *,
    category: str,
    channel: str,
    urgent: bool,
    now: datetime,
) -> Plan:
    """What to do with one message on one channel. See the module docstring."""
    policy = category_policy(category)
    if channel not in prefs.enabled(category):
        return Plan("suppress", "opted_out")
    if channel == "in_app":
        return Plan("send", "in_app")
    if urgent or policy.always_urgent:
        return Plan("send", "urgent", now)
    if prefs.delivery(category) == "daily":
        return Plan("digest", "digest")
    if prefs.snooze_until is not None and prefs.snooze_until > now:
        return Plan("defer", "snoozed", prefs.snooze_until)
    quiet = quiet_until(prefs, now)
    if quiet is not None:
        return Plan("defer", "quiet_hours", quiet)
    return Plan("send", "realtime", now)

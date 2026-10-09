# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""When can a panel meet? Working hours, buffers, daily caps, holidays and time zones.

Everything here is pure: callers pass each person's rules and busy time (from their
calendar and from interviews already booked) and get slots back. Times are timezone
aware ``datetime`` values; every comparison happens in UTC, and local rules (working
hours, holidays, the daily cap) are applied in the person's own time zone, per local
date, through ``zoneinfo``. So a slot is right on both sides of a daylight saving change:
"09:00 to 17:00 in Europe/London" is 09:00 to 17:00 UTC in winter and 08:00 to 16:00 UTC
in summer, and the day of the change is computed from its own offsets.

Local times that do not exist (the hour skipped when clocks go forward) resolve with
``fold=0``, that is with the offset in force before the change; times that happen twice
(the hour repeated when clocks go back) mean their first occurrence. Working hours
rarely fall in those hours, and the result is still a valid instant.

Rules per person (``Rules``):

* ``hours``: working hours per weekday (several ranges a day are allowed, for a lunch
  break); a slot must fit inside one range of its local day.
* ``buffer_before`` and ``buffer_after``: free time needed around the interview; a slot
  whose padded range touches a busy block is refused (reason ``buffer``).
* ``max_per_day``: at most this many interviews on one local day (counting interviews
  already booked, by their local start date).
* ``holidays``: local dates with no interviews at all.

``conflicts`` explains why one slot does not work for each person (shown inline when a
recruiter drags an interview); ``find_slots`` lists every slot where the whole panel is
free, aligned to ``step`` on the UTC clock (so 30 minute steps land on :00 and :30 in
every whole and half hour time zone).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_RANGES_PER_DAY: Final = 4
MAX_HOLIDAYS: Final = 366
MAX_WINDOW: Final = timedelta(days=62)
_HHMM: Final = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$|^24:00$")

ConflictReason = Literal["too_soon", "holiday", "outside_hours", "busy", "buffer", "daily_cap"]


class AvailabilityError(ValueError):
    """Invalid rules or arguments (a bad request, or a programming error)."""


def zone(name: str) -> ZoneInfo:
    """A ``ZoneInfo`` for an IANA name, or ``AvailabilityError``."""
    if not isinstance(name, str) or not name or len(name) > 64:
        raise AvailabilityError("unknown time zone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise AvailabilityError("unknown time zone") from None


def valid_timezone(name: str) -> str:
    zone(name)
    return name


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AvailabilityError("times must carry a time zone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """A half-open range ``[start, end)`` of instants, held in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _aware(self.start))
        object.__setattr__(self, "end", _aware(self.end))
        if self.end <= self.start:
            raise AvailabilityError("an interval ends after it starts")

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: Interval) -> bool:
        return self.start <= other.start and other.end <= self.end

    def padded(self, before: timedelta, after: timedelta) -> Interval:
        return Interval(self.start - before, self.end + after)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


# ------------------------------------------------------------------ rules


def _parse_hhmm(value: str) -> time | None:
    """``"09:30"`` to a time; ``"24:00"`` is None (the end of the day)."""
    if not isinstance(value, str) or not _HHMM.match(value):
        raise AvailabilityError("times are HH:MM")
    if value == "24:00":
        return None
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _fmt(value: time | None) -> str:
    return "24:00" if value is None else f"{value.hour:02d}:{value.minute:02d}"


@dataclass(frozen=True, slots=True)
class DayRange:
    """One working range of a day. ``end`` None means midnight at the end of the day."""

    start: time
    end: time | None

    def __post_init__(self) -> None:
        if self.end is not None and self.end <= self.start:
            raise AvailabilityError("a working range ends after it starts")


@dataclass(frozen=True, slots=True)
class WorkingHours:
    """Working ranges per weekday (0 is Monday). A weekday without ranges is off."""

    days: Mapping[int, tuple[DayRange, ...]]

    def __post_init__(self) -> None:
        for weekday, ranges in self.days.items():
            if weekday not in range(7):
                raise AvailabilityError("weekdays are 0 (Monday) to 6 (Sunday)")
            if len(ranges) > MAX_RANGES_PER_DAY:
                raise AvailabilityError(f"at most {MAX_RANGES_PER_DAY} ranges a day")
            ordered = sorted(ranges, key=lambda r: r.start)
            for a, b in pairwise(ordered):
                if a.end is None or a.end > b.start:
                    raise AvailabilityError("working ranges of a day must not overlap")

    @classmethod
    def standard(
        cls,
        start: time = time(9, 0),
        end: time = time(17, 0),
        weekdays: Iterable[int] = range(5),
    ) -> WorkingHours:
        return cls({d: (DayRange(start, end),) for d in weekdays})

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> WorkingHours:
        """``{"mon": [["09:00", "17:00"]], ...}``; missing days are off."""
        if not isinstance(data, Mapping):
            raise AvailabilityError("working hours are an object of weekdays")
        days: dict[int, tuple[DayRange, ...]] = {}
        for key, ranges in data.items():
            if key not in WEEKDAYS:
                raise AvailabilityError(f"unknown weekday {key!r}")
            if not isinstance(ranges, list):
                raise AvailabilityError("a weekday has a list of ranges")
            parsed: list[DayRange] = []
            for pair in ranges:
                if not isinstance(pair, list | tuple) or len(pair) != 2:
                    raise AvailabilityError("a range is [start, end]")
                start = _parse_hhmm(pair[0])
                if start is None:
                    raise AvailabilityError("a range cannot start at 24:00")
                parsed.append(DayRange(start, _parse_hhmm(pair[1])))
            if parsed:
                days[WEEKDAYS.index(key)] = tuple(parsed)
        return cls(days)

    def to_json(self) -> dict[str, list[list[str]]]:
        return {
            WEEKDAYS[d]: [[_fmt(r.start), _fmt(r.end)] for r in sorted(rs, key=lambda r: r.start)]
            for d, rs in sorted(self.days.items())
        }


STANDARD_HOURS: Final = WorkingHours.standard()


@dataclass(frozen=True, slots=True)
class Rules:
    timezone: str = "UTC"
    hours: WorkingHours = STANDARD_HOURS
    buffer_before: timedelta = timedelta(0)
    buffer_after: timedelta = timedelta(0)
    max_per_day: int | None = None
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        zone(self.timezone)
        if self.buffer_before < timedelta(0) or self.buffer_after < timedelta(0):
            raise AvailabilityError("buffers cannot be negative")
        if self.buffer_before > timedelta(hours=4) or self.buffer_after > timedelta(hours=4):
            raise AvailabilityError("buffers are at most 4 hours")
        if self.max_per_day is not None and not 0 <= self.max_per_day <= 24:
            raise AvailabilityError("max_per_day is 0 to 24")
        if len(self.holidays) > MAX_HOLIDAYS:
            raise AvailabilityError(f"at most {MAX_HOLIDAYS} holidays")

    @property
    def zone(self) -> ZoneInfo:
        return zone(self.timezone)


@dataclass(frozen=True, slots=True)
class Panelist:
    """One person on a panel: their rules and what already fills their calendar.

    ``busy`` is calendar time (any source). ``interviews`` are interviews already booked
    for them; they are busy too, and they count toward ``max_per_day``.
    """

    key: str
    rules: Rules
    busy: tuple[Interval, ...] = ()
    interviews: tuple[Interval, ...] = ()
    _blocked: tuple[Interval, ...] = field(init=False, repr=False, compare=False)
    _starts: tuple[datetime, ...] = field(init=False, repr=False, compare=False)
    _per_day: Mapping[date, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        blocked = merge([*self.busy, *self.interviews])
        object.__setattr__(self, "_blocked", tuple(blocked))
        object.__setattr__(self, "_starts", tuple(b.start for b in blocked))
        tz = self.rules.zone
        per_day: dict[date, int] = {}
        for interview in self.interviews:
            day = interview.start.astimezone(tz).date()
            per_day[day] = per_day.get(day, 0) + 1
        object.__setattr__(self, "_per_day", per_day)

    def overlapping(self, interval: Interval) -> list[Interval]:
        """Blocked intervals overlapping ``interval`` (sorted, merged)."""
        # Blocks are merged, so they do not overlap each other: the first candidate is
        # the last block starting before ``interval`` ends, walking back while they
        # still reach into it.
        index = bisect.bisect_left(self._starts, interval.end)
        found: list[Interval] = []
        while index > 0:
            index -= 1
            block = self._blocked[index]
            if block.end <= interval.start:
                break
            found.append(block)
        return sorted(found)

    def interviews_on(self, day: date) -> int:
        return self._per_day.get(day, 0)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted union of intervals (touching ones are joined)."""
    out: list[Interval] = []
    for current in sorted(intervals):
        if out and current.start <= out[-1].end:
            if current.end > out[-1].end:
                out[-1] = Interval(out[-1].start, current.end)
        else:
            out.append(current)
    return out


def _local(day: date, at: time | None, tz: ZoneInfo) -> datetime:
    if at is None:
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def working_intervals(rules: Rules, window: Interval) -> list[Interval]:
    """The person's working time inside ``window``, in UTC, holidays removed."""
    tz = rules.zone
    first = window.start.astimezone(tz).date() - timedelta(days=1)
    last = window.end.astimezone(tz).date() + timedelta(days=1)
    out: list[Interval] = []
    day = first
    while day <= last:
        if day not in rules.holidays:
            for r in rules.hours.days.get(day.weekday(), ()):
                start, end = _local(day, r.start, tz), _local(day, r.end, tz)
                if end <= start:  # a range swallowed by a clock change
                    continue
                start, end = max(start, window.start), min(end, window.end)
                if start < end:
                    out.append(Interval(start, end))
        day += timedelta(days=1)
    return merge(out)


# ------------------------------------------------------------------ conflicts and slots


@dataclass(frozen=True, slots=True)
class Conflict:
    key: str
    reason: ConflictReason


def conflicts(
    panel: Sequence[Panelist],
    slot: Interval,
    *,
    now: datetime | None = None,
    min_notice: timedelta = timedelta(0),
) -> list[Conflict]:
    """Why ``slot`` does not work, per person (empty: everyone is free).

    ``too_soon`` is reported once, with an empty key, when the slot starts before
    ``now + min_notice``.
    """
    found: list[Conflict] = []
    if now is not None and slot.start < _aware(now) + min_notice:
        found.append(Conflict("", "too_soon"))
    for person in panel:
        reason = _person_conflict(person, slot)
        if reason is not None:
            found.append(Conflict(person.key, reason))
    return found


def _person_conflict(person: Panelist, slot: Interval) -> ConflictReason | None:
    rules = person.rules
    tz = rules.zone
    local_day = slot.start.astimezone(tz).date()
    if local_day in rules.holidays:
        return "holiday"
    hours = working_intervals(rules, Interval(slot.start - timedelta(days=1), slot.end))
    if not any(h.contains(slot) for h in hours):
        return "outside_hours"
    if person.overlapping(slot):
        return "busy"
    if (rules.buffer_before or rules.buffer_after) and person.overlapping(
        slot.padded(rules.buffer_before, rules.buffer_after)
    ):
        return "buffer"
    if rules.max_per_day is not None and person.interviews_on(local_day) >= rules.max_per_day:
        return "daily_cap"
    return None


def _align(value: datetime, step: timedelta) -> datetime:
    """The first instant at or after ``value`` that is a whole number of steps since the
    UTC epoch."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    step_us = step // timedelta(microseconds=1)
    elapsed = (value - epoch) // timedelta(microseconds=1)
    remainder = elapsed % step_us
    if remainder == 0:
        return value
    return epoch + timedelta(microseconds=elapsed - remainder + step_us)


def find_slots(
    panel: Sequence[Panelist],
    window: Interval,
    duration: timedelta,
    *,
    step: timedelta = timedelta(minutes=30),
    now: datetime | None = None,
    min_notice: timedelta = timedelta(0),
    limit: int = 500,
) -> list[Interval]:
    """Every slot of ``duration`` inside ``window`` where the whole panel is free."""
    if not panel:
        raise AvailabilityError("a panel has at least one person")
    if not timedelta(minutes=5) <= duration <= timedelta(hours=8):
        raise AvailabilityError("an interview lasts 5 minutes to 8 hours")
    if not timedelta(minutes=5) <= step <= timedelta(hours=2) or step.total_seconds() % 60:
        raise AvailabilityError("the step is 5 minutes to 2 hours, in whole minutes")
    if window.duration > MAX_WINDOW:
        raise AvailabilityError("the search window is at most 62 days")
    start = window.start
    if now is not None:
        start = max(start, _aware(now) + min_notice)
    # Only candidate starts inside the intersection of everyone's working time are tried.
    shared = working_intervals(panel[0].rules, window)
    for person in panel[1:]:
        shared = _intersect(shared, working_intervals(person.rules, window))
    out: list[Interval] = []
    for free in shared:
        at = _align(max(free.start, start), step)
        while at + duration <= free.end:
            slot = Interval(at, at + duration)
            if all(_person_conflict(p, slot) is None for p in panel):
                out.append(slot)
                if len(out) >= limit:
                    return out
            at += step
    return out


def _intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start, end = max(a[i].start, b[j].start), min(a[i].end, b[j].end)
        if start < end:
            out.append(Interval(start, end))
        if a[i].end < b[j].end:
            i += 1
        else:
            j += 1
    return out


def spread(
    slots: Sequence[Interval], tz: str, *, per_day: int = 4, days: int = 5
) -> list[Interval]:
    """A short list to suggest: at most ``per_day`` slots on each of the first ``days``
    local days (in ``tz``) that have any, spaced out across each day."""
    zone_ = zone(tz)
    by_day: dict[date, list[Interval]] = {}
    for slot in slots:
        by_day.setdefault(slot.start.astimezone(zone_).date(), []).append(slot)
    out: list[Interval] = []
    for day in sorted(by_day)[:days]:
        items = by_day[day]
        if len(items) <= per_day:
            out.extend(items)
            continue
        stride = (len(items) - 1) / max(per_day - 1, 1)
        picked = sorted({round(i * stride) for i in range(per_day)})
        out.extend(items[i] for i in picked)
    return out


def sociable(slot: Interval, tz: str, *, earliest: time = time(8), latest: time = time(20)) -> bool:
    """Whether the slot falls between ``earliest`` and ``latest`` local time in ``tz``
    (a candidate far away may still be offered an early or late slot, marked as such)."""
    zone_ = zone(tz)
    start = slot.start.astimezone(zone_)
    end = slot.end.astimezone(zone_)
    if end.date() != start.date():
        return False
    return earliest <= start.time() and end.time() <= latest

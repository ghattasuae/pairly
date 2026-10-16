# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Availability math: working hours across time zones and DST, panels, buffers, caps."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pairly_core.scheduling.availability import (
    AvailabilityError,
    DayRange,
    Interval,
    Panelist,
    Rules,
    WorkingHours,
    conflicts,
    find_slots,
    merge,
    sociable,
    spread,
    valid_timezone,
    working_intervals,
)

LONDON = "Europe/London"
DUBAI = "Asia/Dubai"
NEW_YORK = "America/New_York"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def local(tz: str, year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


def day_window(day: date) -> Interval:
    """The UTC day (every zone in these tests works inside it)."""
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return Interval(start, start + timedelta(days=1))


def test_interval_needs_aware_and_ordered_times() -> None:
    with pytest.raises(AvailabilityError):
        Interval(datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10))
    with pytest.raises(AvailabilityError):
        Interval(utc(2026, 1, 1, 10), utc(2026, 1, 1, 9))
    a = Interval(local(DUBAI, 2026, 1, 1, 13), local(DUBAI, 2026, 1, 1, 14))
    assert a.start == utc(2026, 1, 1, 9)  # held in UTC
    assert a.overlaps(Interval(utc(2026, 1, 1, 9, 30), utc(2026, 1, 1, 11)))
    assert not a.overlaps(Interval(utc(2026, 1, 1, 10), utc(2026, 1, 1, 11)))  # half open


def test_working_hours_json_round_trip_and_validation() -> None:
    hours = WorkingHours.from_json(
        {"mon": [["09:00", "12:00"], ["13:00", "17:30"]], "sat": [["10:00", "24:00"]]}
    )
    assert hours.to_json() == {
        "mon": [["09:00", "12:00"], ["13:00", "17:30"]],
        "sat": [["10:00", "24:00"]],
    }
    for bad in (
        {"funday": [["09:00", "10:00"]]},
        {"mon": [["9:00", "10:00"]]},
        {"mon": [["10:00", "09:00"]]},
        {"mon": [["09:00", "12:00"], ["11:00", "13:00"]]},
        {"mon": [["24:00", "24:00"]]},
        {"mon": "09:00-17:00"},
    ):
        with pytest.raises(AvailabilityError):
            WorkingHours.from_json(bad)
    with pytest.raises(AvailabilityError):
        Rules(timezone="Mars/Olympus")
    with pytest.raises(AvailabilityError):
        Rules(buffer_before=timedelta(minutes=-5))
    with pytest.raises(AvailabilityError):
        Rules(max_per_day=99)
    assert valid_timezone(DUBAI) == DUBAI


def test_london_working_day_follows_dst() -> None:
    rules = Rules(timezone=LONDON)
    # Winter (GMT): 09:00 to 17:00 local is 09:00 to 17:00 UTC.
    (winter,) = working_intervals(rules, day_window(date(2026, 3, 27)))
    assert (winter.start, winter.end) == (utc(2026, 3, 27, 9), utc(2026, 3, 27, 17))
    # After clocks go forward on Sunday 29 March 2026 (BST): 08:00 to 16:00 UTC.
    (summer,) = working_intervals(rules, day_window(date(2026, 3, 30)))
    assert (summer.start, summer.end) == (utc(2026, 3, 30, 8), utc(2026, 3, 30, 16))
    # Back to GMT after Sunday 25 October 2026.
    (autumn,) = working_intervals(rules, day_window(date(2026, 10, 26)))
    assert (autumn.start, autumn.end) == (utc(2026, 10, 26, 9), utc(2026, 10, 26, 17))


def test_day_of_the_change_uses_its_own_offsets() -> None:
    # A Sunday shift 00:00 to 06:00 on the night clocks go forward lasts 5 real hours,
    # and 7 on the night they go back.
    rules = Rules(timezone=LONDON, hours=WorkingHours({6: (DayRange(time(0), time(6)),)}))
    wide = timedelta(hours=6)
    spring_day = day_window(date(2026, 3, 29))
    (spring,) = working_intervals(rules, Interval(spring_day.start - wide, spring_day.end))
    assert spring.duration == timedelta(hours=5)
    autumn_day = day_window(date(2026, 10, 25))
    (autumn,) = working_intervals(rules, Interval(autumn_day.start - wide, autumn_day.end))
    assert autumn.duration == timedelta(hours=7)


def test_dubai_has_no_dst_and_new_york_does() -> None:
    dubai = Rules(timezone=DUBAI)
    for day in (date(2026, 1, 5), date(2026, 7, 6)):
        (block,) = working_intervals(dubai, day_window(day))
        assert block.start.hour == 5
        assert block.end.hour == 13
    ny = Rules(timezone=NEW_YORK)
    (winter,) = working_intervals(ny, day_window(date(2026, 3, 6)))
    (summer,) = working_intervals(ny, day_window(date(2026, 3, 9)))  # after 8 March
    assert winter.start.hour == 14
    assert summer.start.hour == 13


def _panel(*people: tuple[str, Rules]) -> list[Panelist]:
    return [Panelist(key, rules) for key, rules in people]


def test_panel_intersection_between_london_and_dubai_moves_with_uk_dst() -> None:
    panel = _panel(("london", Rules(timezone=LONDON)), ("dubai", Rules(timezone=DUBAI)))
    hour = timedelta(hours=1)
    # Friday 27 March 2026: London is UTC+0, so the shared time is 09:00 to 13:00 UTC.
    slots = find_slots(panel, day_window(date(2026, 3, 27)), hour, step=hour)
    assert [s.start.hour for s in slots] == [9, 10, 11, 12]
    # Monday 30 March 2026: London is UTC+1, shared time 08:00 to 13:00 UTC.
    slots = find_slots(panel, day_window(date(2026, 3, 30)), hour, step=hour)
    assert [s.start.hour for s in slots] == [8, 9, 10, 11, 12]
    # Every slot is inside both people's local working hours.
    for slot in slots:
        assert 9 <= slot.start.astimezone(ZoneInfo(LONDON)).hour < 17
        assert 9 <= slot.start.astimezone(ZoneInfo(DUBAI)).hour < 17


def test_slots_shown_in_the_candidates_zone_between_us_and_uk_dst() -> None:
    # Between 8 March (US clocks forward) and 29 March (UK clocks forward) 2026, New York
    # is 4 hours behind London instead of 5. A London slot at 14:00 is 10:00 in New York.
    panel = _panel(("london", Rules(timezone=LONDON)))
    window = Interval(local(LONDON, 2026, 3, 16, 14), local(LONDON, 2026, 3, 16, 15))
    (slot,) = find_slots(panel, window, timedelta(hours=1))
    assert slot.start.astimezone(ZoneInfo(NEW_YORK)).hour == 10
    window = Interval(local(LONDON, 2026, 4, 6, 14), local(LONDON, 2026, 4, 6, 15))
    (slot,) = find_slots(panel, window, timedelta(hours=1))
    assert slot.start.astimezone(ZoneInfo(NEW_YORK)).hour == 9
    assert sociable(slot, NEW_YORK)
    assert not sociable(Interval(utc(2026, 4, 6, 9), utc(2026, 4, 6, 10)), NEW_YORK)  # 05:00


def test_busy_time_buffers_and_daily_cap() -> None:
    day = date(2026, 10, 12)  # a Monday
    meeting = Interval(local(DUBAI, 2026, 10, 12, 11), local(DUBAI, 2026, 10, 12, 12))
    booked = (
        Interval(local(DUBAI, 2026, 10, 12, 14), local(DUBAI, 2026, 10, 12, 15)),
        Interval(local(DUBAI, 2026, 10, 12, 15, 30), local(DUBAI, 2026, 10, 12, 16)),
    )
    rules = Rules(
        timezone=DUBAI,
        buffer_before=timedelta(minutes=15),
        buffer_after=timedelta(minutes=15),
        max_per_day=3,
    )
    person = Panelist("p", rules, busy=(meeting,), interviews=booked)
    slots = find_slots([person], day_window(day), timedelta(minutes=30))
    starts = {s.start.astimezone(ZoneInfo(DUBAI)).strftime("%H:%M") for s in slots}
    # Busy 11:00 to 12:00 plus 15 minute buffers: 10:30 and 12:00 are too close.
    assert "10:30" not in starts
    assert "12:00" not in starts
    assert "11:00" not in starts
    assert "10:00" in starts
    assert "12:30" in starts
    assert "13:30" not in starts  # buffer before the 14:00 interview
    assert "16:30" in starts

    def at(hour: int, minute: int = 0) -> Interval:
        start = local(DUBAI, 2026, 10, 12, hour, minute)
        return Interval(start, start + timedelta(minutes=30))

    assert conflicts([person], at(11)) == [conflicts([person], at(11))[0]]
    assert conflicts([person], at(11))[0].reason == "busy"
    assert conflicts([person], at(10, 30))[0].reason == "buffer"
    assert conflicts([person], at(8))[0].reason == "outside_hours"
    assert conflicts([person], at(16, 30)) == []
    # A third interview fills the day: the cap is 3.
    capped = Panelist("p", rules, interviews=(*booked, at(9)))
    assert conflicts([capped], at(16, 30))[0].reason == "daily_cap"
    assert find_slots([capped], day_window(day), timedelta(minutes=30)) == []
    # The cap counts per local day: the next day is free again.
    assert find_slots([capped], day_window(day + timedelta(days=1)), timedelta(minutes=30))


def test_daily_cap_counts_the_local_date() -> None:
    # 23:30 UTC on Sunday is 03:30 on Monday in Dubai: it counts toward Monday.
    rules = Rules(timezone=DUBAI, max_per_day=1, hours=WorkingHours.standard(time(0), time(23)))
    late = Interval(utc(2026, 10, 11, 23, 30), utc(2026, 10, 12, 0))
    person = Panelist("p", rules, interviews=(late,))
    monday = Interval(local(DUBAI, 2026, 10, 12, 10), local(DUBAI, 2026, 10, 12, 11))
    assert conflicts([person], monday)[0].reason == "daily_cap"


def test_holidays_and_minimum_notice() -> None:
    rules = Rules(timezone=DUBAI, holidays=frozenset({date(2026, 12, 2)}))
    person = [Panelist("p", rules)]
    holiday = Interval(local(DUBAI, 2026, 12, 2, 10), local(DUBAI, 2026, 12, 2, 11))
    assert conflicts(person, holiday)[0].reason == "holiday"
    assert find_slots(person, day_window(date(2026, 12, 2)), timedelta(hours=1)) == []
    assert find_slots(person, day_window(date(2026, 12, 3)), timedelta(hours=1))
    now = local(DUBAI, 2026, 12, 3, 10, 10)
    slots = find_slots(
        person,
        day_window(date(2026, 12, 3)),
        timedelta(hours=1),
        now=now,
        min_notice=timedelta(hours=2),
    )
    assert slots[0].start == local(DUBAI, 2026, 12, 3, 12, 30)
    too_soon = conflicts(person, holiday, now=now)
    assert too_soon[0].key == ""
    assert too_soon[0].reason == "too_soon"


def test_a_three_person_panel_needs_everyone() -> None:
    day = date(2026, 10, 13)
    london = Panelist(
        "london",
        Rules(timezone=LONDON),
        busy=(Interval(local(LONDON, 2026, 10, 13, 9), local(LONDON, 2026, 10, 13, 10)),),
    )
    dubai = Panelist("dubai", Rules(timezone=DUBAI))
    ny = Panelist("ny", Rules(timezone=NEW_YORK, hours=WorkingHours.standard(time(7), time(15))))
    hour = timedelta(hours=1)
    # London is UTC+1 (BST), Dubai UTC+4, New York UTC-4 (EDT): the only common time is
    # 11:00 to 13:00 UTC (New York from 07:00, Dubai until 17:00).
    slots = find_slots([london, dubai, ny], day_window(day), hour, step=hour)
    assert [s.start for s in slots] == [utc(2026, 10, 13, 11), utc(2026, 10, 13, 12)]
    assert find_slots([london, dubai, ny], day_window(day), timedelta(hours=3)) == []


def test_slots_align_to_half_hours_and_spread() -> None:
    person = [Panelist("p", Rules(timezone="Asia/Kolkata"))]  # UTC+5:30
    slots = find_slots(person, day_window(date(2026, 10, 14)), timedelta(minutes=45))
    assert slots
    assert all(s.start.minute in (0, 30) for s in slots)
    assert slots[0].start.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M") == "09:00"
    week = Interval(utc(2026, 10, 12), utc(2026, 10, 19))
    many = find_slots(person, week, timedelta(minutes=45))
    picked = spread(many, "Asia/Kolkata", per_day=3, days=2)
    assert len(picked) == 6
    assert picked[0] == many[0]


def test_limits_and_merge() -> None:
    person = [Panelist("p", Rules())]
    window = Interval(utc(2026, 10, 12), utc(2026, 10, 13))
    with pytest.raises(AvailabilityError):
        find_slots([], window, timedelta(hours=1))
    with pytest.raises(AvailabilityError):
        find_slots(person, window, timedelta(minutes=1))
    with pytest.raises(AvailabilityError):
        find_slots(person, window, timedelta(hours=1), step=timedelta(seconds=90))
    with pytest.raises(AvailabilityError):
        find_slots(person, Interval(utc(2026, 1, 1), utc(2026, 6, 1)), timedelta(hours=1))
    assert len(find_slots(person, window, timedelta(minutes=30), limit=3)) == 3
    merged = merge(
        [
            Interval(utc(2026, 1, 1, 9), utc(2026, 1, 1, 10)),
            Interval(utc(2026, 1, 1, 10), utc(2026, 1, 1, 11)),
            Interval(utc(2026, 1, 1, 9, 30), utc(2026, 1, 1, 9, 45)),
            Interval(utc(2026, 1, 1, 12), utc(2026, 1, 1, 13)),
        ]
    )
    assert merged == [
        Interval(utc(2026, 1, 1, 9), utc(2026, 1, 1, 11)),
        Interval(utc(2026, 1, 1, 12), utc(2026, 1, 1, 13)),
    ]

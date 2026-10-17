# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Calendar files (RFC 5545), written by hand: no third party library.

``build_ics`` makes one ``VCALENDAR`` with one ``VEVENT``:

* ``METHOD:REQUEST`` for an invitation or a change, ``METHOD:CANCEL`` (with
  ``STATUS:CANCELLED``) for a cancellation (RFC 5546, iTIP).
* ``UID`` stays the same for the life of the meeting; ``SEQUENCE`` grows with every
  change the attendee must see (a new time, a cancellation), so calendar apps replace
  the event they already have instead of adding a second one.
* Times are UTC (``DTSTART:20261013T060000Z``), which RFC 5545 allows without a
  ``VTIMEZONE``; calendar apps show them in the reader's own zone.
* Text values are escaped (backslash, semicolon, comma, new lines), every line ends
  with CRLF and is folded at 75 octets without splitting a UTF-8 character.
* Header-like values (names, emails, the UID) are reduced to one line, so a value can
  never add a property.

``read_ics`` unfolds and splits a file into its events' properties: for checks and
tests, not a general parser.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

Method = Literal["REQUEST", "CANCEL", "PUBLISH"]

PRODID: Final = "-//Proofly Technology//Pairly//EN"
MAX_LINE_OCTETS: Final = 75
_EMAIL_RE: Final = re.compile(r"^[^@\s<>\"':;,]{1,64}@[A-Za-z0-9.-]{1,253}$")
_UID_RE: Final = re.compile(r"^[A-Za-z0-9._@-]{8,200}$")


class IcsError(ValueError):
    """An event that cannot be written (bad UID, email or times)."""


@dataclass(frozen=True, slots=True)
class IcsPerson:
    email: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class IcsEvent:
    uid: str
    sequence: int
    start: datetime
    end: datetime
    summary: str
    organizer: IcsPerson
    attendees: Sequence[IcsPerson] = ()
    description: str = ""
    location: str = ""
    url: str | None = None


def escape_text(value: str) -> str:
    """RFC 5545 3.3.11 TEXT: escape backslash, semicolon and comma; new lines as ``\\n``."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _one_line(value: str, limit: int = 200) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:limit]


def _param(value: str) -> str:
    """A parameter value (``CN=``): quoted, without quotes, colons, semicolons or
    control characters (so a reader splitting at the first colon stays right)."""
    clean = "".join(c for c in _one_line(value, 120) if c not in '":;' and ord(c) >= 0x20)
    return f'"{clean}"'


def _email(value: str) -> str:
    clean = _one_line(value, 320)
    if not _EMAIL_RE.match(clean):
        raise IcsError("invalid email address")
    return clean


def utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IcsError("times must carry a time zone")
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 3.1), never inside a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= MAX_LINE_OCTETS:
        return line
    parts: list[str] = []
    current = ""
    size = 0
    limit = MAX_LINE_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current, size = "", 0
            limit = MAX_LINE_OCTETS - 1  # continuation lines start with one space
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _person(prop: str, person: IcsPerson, extra: str = "") -> str:
    params = f";CN={_param(person.name)}" if person.name else ""
    return f"{prop}{params}{extra}:mailto:{_email(person.email)}"


def build_ics(event: IcsEvent, method: Method, *, stamp: datetime | None = None) -> str:
    """The calendar file for ``event`` (see the module docstring)."""
    if not _UID_RE.match(event.uid):
        raise IcsError("invalid UID")
    if not 0 <= event.sequence <= 10_000:
        raise IcsError("invalid SEQUENCE")
    if event.end <= event.start:
        raise IcsError("an event ends after it starts")
    cancel = method == "CANCEL"
    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"SEQUENCE:{event.sequence}",
        f"DTSTAMP:{utc_stamp(stamp or datetime.now(UTC))}",
        f"DTSTART:{utc_stamp(event.start)}",
        f"DTEND:{utc_stamp(event.end)}",
        f"SUMMARY:{escape_text(_one_line(event.summary, 300))}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{escape_text(event.description[:4000])}")
    if event.location:
        lines.append(f"LOCATION:{escape_text(_one_line(event.location, 500))}")
    if event.url and re.match(r"^https?://", event.url):
        lines.append(f"URL:{_one_line(event.url, 900)}")
    lines.append(_person("ORGANIZER", event.organizer))
    for attendee in event.attendees:
        rsvp = "" if cancel else ";RSVP=TRUE"
        lines.append(
            _person(
                "ATTENDEE",
                attendee,
                f";ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION{rsvp}",
            )
        )
    lines += [
        f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


# ------------------------------------------------------------------ reading (checks)


def unfold(text: str) -> list[str]:
    """Content lines with folding removed (RFC 5545 3.1)."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class IcsFile:
    """A file as properties: ``calendar`` (``METHOD``, ``VERSION``...) and one dict per
    ``VEVENT`` mapping a property name to its values (``ATTENDEE`` repeats)."""

    calendar: dict[str, list[str]]
    events: list[dict[str, list[str]]]


def read_ics(text: str) -> IcsFile:
    calendar: dict[str, list[str]] = {}
    events: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    for line in unfold(text):
        name_part, _, value = line.partition(":")
        name = name_part.split(";", 1)[0].upper()
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = {}
            continue
        if name == "END" and value.upper() == "VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        target = current if current is not None else calendar
        target.setdefault(name, []).append(line[len(name_part) + 1 :])
    return IcsFile(calendar, events)

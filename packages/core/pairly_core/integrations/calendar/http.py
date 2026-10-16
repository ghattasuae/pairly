# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Small HTTP helpers shared by the Microsoft Graph and Zoom providers.

Status codes become ``CalendarError``: 401 and 403 are ``<prefix>_unauthorized`` (not
transient: the person or the administrator reconnects), 429 and 5xx are transient,
anything else is not. A body that is not a JSON object is a transient bad response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from pairly_core.integrations.calendar.base import CalendarError


def status_error(response: httpx.Response, prefix: str) -> CalendarError:
    code = response.status_code
    if code in (401, 403):
        return CalendarError(f"{prefix}_unauthorized", transient=False)
    if code == 429 or code >= 500:
        return CalendarError(f"{prefix}_{code}", transient=True)
    return CalendarError(f"{prefix}_{code}", transient=False)


def json_object(response: httpx.Response, prefix: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise CalendarError(f"{prefix}_bad_response", transient=True) from None
    if not isinstance(data, dict):
        raise CalendarError(f"{prefix}_bad_response", transient=True)
    return data


def utc_z(value: datetime) -> str:
    """``2026-10-13T05:00:00Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_naive(value: datetime) -> str:
    """``2026-10-13T05:00:00``, read with ``timeZone: UTC`` (Graph's dateTimeTimeZone)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Signed self-scheduling links (``hs1``) and Google connect state (``hg1``).

Both are HMAC-SHA256 under ``PAIRLY_SIGNED_LINK_SECRET`` with a purpose label in the MAC
(the scheme of ``pairly_hire.links``), compared in constant time.

``hs1.<org>.<interview>.<application>.<stage>.<nonce>.<expires>.<mac>`` opens one
interview of one application at one stage. It is single use per booking: the
interview row keeps the current ``link_nonce``, and every booking replaces it, so a
link that booked once never books again (it reads as ``used``). The candidate's
confirmation carries the next link (to change or cancel). ``expires`` is a Unix time:
a link to choose a time lasts the organisation's ``link_days``; a link to change a
booked time lasts until the interview starts.

``hg1.<org>.<user>.<issued>.<mac>`` is the ``state`` of a Google OAuth round trip: it
binds the callback to the person who started it, for ten minutes.

A garbled, forged or expired token reads as ``None``; the reason is never shown.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

SCHEDULE_PREFIX: Final = "hs1"
CONNECT_PREFIX: Final = "hg1"
MAX_TOKEN: Final = 300
CONNECT_MAX_AGE: Final = 600
_STAGE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NONCE: Final = re.compile(r"^[0-9a-f]{16}$")
_SKEW: Final = 300


def new_nonce() -> str:
    return secrets.token_hex(8)


def _mac(secret: str, purpose: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"pairly.hire.{purpose}|{body}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _uuid(value: str) -> uuid.UUID | None:
    if len(value) != 32:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def _int(value: str) -> int | None:
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        return None
    return int(value)


@dataclass(frozen=True, slots=True)
class ScheduleClaim:
    org_id: uuid.UUID
    interview_id: uuid.UUID
    application_id: uuid.UUID
    stage_key: str
    nonce: str
    expires_at: datetime


def schedule_token(
    secret: str,
    *,
    org_id: uuid.UUID,
    interview_id: uuid.UUID,
    application_id: uuid.UUID,
    stage_key: str,
    nonce: str,
    expires_at: datetime,
) -> str:
    if not _STAGE.match(stage_key) or not _NONCE.match(nonce):
        raise ValueError("invalid stage or nonce")
    body = ".".join(
        (
            SCHEDULE_PREFIX,
            org_id.hex,
            interview_id.hex,
            application_id.hex,
            stage_key,
            nonce,
            str(int(expires_at.timestamp())),
        )
    )
    return f"{body}.{_mac(secret, 'schedule', body)}"


def read_schedule_token(
    secret: str, token: str, *, now: float | None = None
) -> ScheduleClaim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 8 or parts[0] != SCHEDULE_PREFIX:
        return None
    body = ".".join(parts[:7])
    if not hmac.compare_digest(_mac(secret, "schedule", body), parts[7]):
        return None
    org_id, interview_id, application_id = _uuid(parts[1]), _uuid(parts[2]), _uuid(parts[3])
    stage, nonce, expires = parts[4], parts[5], _int(parts[6])
    if org_id is None or interview_id is None or application_id is None or expires is None:
        return None
    if not _STAGE.match(stage) or not _NONCE.match(nonce):
        return None
    if (time.time() if now is None else now) > expires:
        return None
    return ScheduleClaim(
        org_id,
        interview_id,
        application_id,
        stage,
        nonce,
        datetime.fromtimestamp(expires, tz=UTC),
    )


@dataclass(frozen=True, slots=True)
class ConnectClaim:
    org_id: uuid.UUID
    user_id: uuid.UUID


def connect_state(secret: str, org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    body = f"{CONNECT_PREFIX}.{org_id.hex}.{user_id.hex}.{int(time.time())}"
    return f"{body}.{_mac(secret, 'calendar-connect', body)}"


def read_connect_state(secret: str, token: str, *, now: float | None = None) -> ConnectClaim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != CONNECT_PREFIX:
        return None
    body = ".".join(parts[:4])
    if not hmac.compare_digest(_mac(secret, "calendar-connect", body), parts[4]):
        return None
    org_id, user_id, issued = _uuid(parts[1]), _uuid(parts[2]), _int(parts[3])
    if org_id is None or user_id is None or issued is None:
        return None
    age = (time.time() if now is None else now) - issued
    if age < -_SKEW or age > CONNECT_MAX_AGE:
        return None
    return ConnectClaim(org_id, user_id)

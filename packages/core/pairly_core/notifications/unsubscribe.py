# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""One-click unsubscribe tokens (RFC 8058) for non-transactional email.

``v1.<org>.<user>.<category>.<issued>.<mac>``: ``issued`` is the Unix time the token was
made, and the MAC is HMAC-SHA256 with ``PAIRLY_SIGNED_LINK_SECRET`` over the other
fields, compared in constant time. A token can only turn email off for one category of
one person in one organisation. It expires after ``MAX_AGE_SECONDS`` (60 days: opting
out must keep working for at least 30 days after a message is sent), so a leaked old
token stops working. It is not single use: unsubscribing twice changes nothing.

The token travels in the ``List-Unsubscribe`` header and in the query string of the
one-click URL (``POST /v1/notifications/unsubscribe?token=...``): RFC 8058 needs the
POST to carry everything in the URL. The API access log leaves query strings out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PREFIX = "v1"
MAX_AGE_SECONDS = 60 * 24 * 3600
# Clock skew allowed for a token issued "in the future" by another server.
_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class UnsubscribeClaim:
    org_id: uuid.UUID
    user_id: uuid.UUID
    category: str


def _mac(secret: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"pairly.unsubscribe|{body}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_token(secret: str, claim: UnsubscribeClaim, *, now: float | None = None) -> str:
    if not _CATEGORY_RE.fullmatch(claim.category):
        raise ValueError("invalid category")
    issued = int(time.time() if now is None else now)
    body = f"{_PREFIX}.{claim.org_id.hex}.{claim.user_id.hex}.{claim.category}.{issued}"
    return f"{body}.{_mac(secret, body)}"


def read_token(
    secret: str,
    token: str,
    *,
    now: float | None = None,
    max_age_seconds: int = MAX_AGE_SECONDS,
) -> UnsubscribeClaim | None:
    """The claim of a valid, unexpired token, or None. Never raises on malformed input."""
    if not isinstance(token, str) or len(token) > 200:
        return None
    parts = token.split(".")
    if len(parts) != 6 or parts[0] != _PREFIX:
        return None
    body, mac = ".".join(parts[:5]), parts[5]
    if not hmac.compare_digest(_mac(secret, body), mac):
        return None
    try:
        org_id, user_id = uuid.UUID(hex=parts[1]), uuid.UUID(hex=parts[2])
    except ValueError:
        return None
    if not _CATEGORY_RE.fullmatch(parts[3]):
        return None
    issued = parts[4]
    if not issued.isascii() or not issued.isdigit() or len(issued) > 12:
        return None
    current = time.time() if now is None else now
    age = current - int(issued)
    if age > max_age_seconds or age < -_SKEW_SECONDS:
        return None
    return UnsubscribeClaim(org_id, user_id, parts[3])

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Slack request signing, version ``v0``.

Slack signs every request with the app's signing secret:

    X-Slack-Request-Timestamp: <unix seconds>
    X-Slack-Signature: v0=<hex HMAC-SHA256(secret, "v0:<timestamp>:<raw body>")>

``verify`` recomputes the HMAC over the exact raw body bytes (before any form or JSON
parsing), compares in constant time (``hmac.compare_digest``), and refuses timestamps
more than five minutes away from our clock, so a captured request cannot be replayed
later. Every failure is the same ``SlackSignatureError`` (the reason is only logged).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

VERSION: Final = "v0"
DEFAULT_WINDOW_SECONDS: Final = 300
MAX_BODY_BYTES: Final = 256 * 1024


class SlackSignatureError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def compute(signing_secret: str, timestamp: str, body: bytes) -> str:
    base = f"{VERSION}:{timestamp}:".encode("ascii") + body
    mac = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{VERSION}={mac}"


def verify(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> None:
    """Raise ``SlackSignatureError`` unless the request is signed and fresh."""
    if not signing_secret:
        raise SlackSignatureError("no_secret")
    if len(body) > MAX_BODY_BYTES:
        raise SlackSignatureError("body_too_large")
    if not timestamp or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
        raise SlackSignatureError("bad_timestamp")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > window_seconds:
        raise SlackSignatureError("stale_timestamp")
    if not signature or not signature.isascii() or not signature.startswith(f"{VERSION}="):
        raise SlackSignatureError("bad_signature")
    expected = compute(signing_secret, timestamp, body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        raise SlackSignatureError("signature_mismatch")

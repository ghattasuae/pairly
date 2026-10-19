# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Resend webhook signatures (Svix scheme).

Resend signs every webhook with the endpoint's signing secret (``whsec_<base64 key>``):

    svix-id:        <message id, the same on every retry of one event>
    svix-timestamp: <unix seconds>
    svix-signature: v1,<base64 HMAC-SHA256(key, "<id>.<timestamp>.<raw body>")> [v1,<...>]

``verify`` recomputes the HMAC over the exact raw body bytes (before any JSON parsing),
compares it in constant time (``hmac.compare_digest``) with every ``v1`` signature in the
header (several during a secret rotation), and refuses timestamps more than five minutes
from our clock, so a captured request cannot be replayed later. A replay inside the window
is caught by the caller's dedupe on ``svix-id`` (``pairly_core.webhooks.inbound``). Every
failure is the same ``ResendSignatureError``; the reason is only logged.

Sources, read 2026-10-19: https://resend.com/docs/dashboard/webhooks/verify-webhooks-requests,
https://docs.svix.com/receiving/verifying-payloads/how-manual.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from typing import Final

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 (a prefix, not a secret)
DEFAULT_WINDOW_SECONDS: Final = 300
MAX_BODY_BYTES: Final = 256 * 1024
_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ResendSignatureError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decode_secret(secret: str) -> bytes:
    """The HMAC key from a ``whsec_`` secret. Raises ``ValueError`` when malformed."""
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("a Resend webhook secret starts with whsec_")
    try:
        key = base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the Resend webhook secret is not valid base64") from None
    if len(key) < 16:
        raise ValueError("the Resend webhook secret is too short")
    return key


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """The ``v1,<signature>`` value for one message (tests and tooling)."""
    content = f"{msg_id}.{timestamp}.".encode("ascii") + body
    digest = hmac.new(decode_secret(secret), content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify(
    *,
    secret: str,
    msg_id: str | None,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Return the message id when the request is signed and fresh, else raise."""
    try:
        key = decode_secret(secret)
    except ValueError:
        raise ResendSignatureError("no_secret") from None
    if len(body) > MAX_BODY_BYTES:
        raise ResendSignatureError("body_too_large")
    if not msg_id or not _ID_RE.fullmatch(msg_id):
        raise ResendSignatureError("bad_id")
    if not timestamp or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
        raise ResendSignatureError("bad_timestamp")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > window_seconds:
        raise ResendSignatureError("stale_timestamp")
    if not signature or not signature.isascii() or len(signature) > 2048:
        raise ResendSignatureError("bad_signature")
    content = f"{msg_id}.{timestamp}.".encode("ascii") + body
    expected = base64.b64encode(hmac.new(key, content, hashlib.sha256).digest())
    matched = False
    for entry in signature.split(" "):
        version, _, value = entry.partition(",")
        # No early exit: every candidate is compared.
        if version == "v1" and hmac.compare_digest(expected, value.encode("ascii")):
            matched = True
    if not matched:
        raise ResendSignatureError("signature_mismatch")
    return msg_id

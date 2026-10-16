# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhook signatures. Standard library only: the Python and TypeScript SDKs mirror it.

Every outbound webhook carries::

    Pairly-Signature: t=<unix seconds>,v1=<hex>

where ``<hex>`` is ``HMAC-SHA256(secret, "<t>." + body)`` over the exact request body
bytes, keyed with the endpoint's signing secret (``whsec_...``, as UTF-8). Receivers
check it with ``verify_webhook_signature``: the timestamp must be within ``tolerance``
seconds of now (replay protection) and one ``v1`` value must match (several are
allowed, for secret rotation). Comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

SIGNATURE_HEADER: Final = "Pairly-Signature"
SCHEME: Final = "v1"
DEFAULT_TOLERANCE_SECONDS: Final = 300
MAX_HEADER_LENGTH: Final = 4096


class WebhookSignatureError(ValueError):
    """The signature header is missing, malformed, too old, or does not match."""


def _as_bytes(payload: bytes | str) -> bytes:
    return payload.encode("utf-8") if isinstance(payload, str) else payload


def compute_signature(secret: str, timestamp: int, payload: bytes | str) -> str:
    """Hex ``HMAC-SHA256(secret, "<timestamp>." + payload)``."""
    signed = str(int(timestamp)).encode("ascii") + b"." + _as_bytes(payload)
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_payload(secret: str, payload: bytes | str, timestamp: int | None = None) -> str:
    """The ``Pairly-Signature`` header value for ``payload``."""
    t = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={t},{SCHEME}={compute_signature(secret, t, payload)}"


def verify_webhook_signature(
    payload: bytes | str,
    header: str | None,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    *,
    now: int | None = None,
) -> int:
    """Check a ``Pairly-Signature`` header. Returns its timestamp, or raises.

    ``payload`` must be the raw request body, before any JSON parsing. Raises
    ``WebhookSignatureError`` when the header is missing or malformed, the timestamp is
    further than ``tolerance`` seconds from now (either way), or no ``v1`` signature
    matches. Never ignore the exception: it is the only failure signal.
    """
    if not secret:
        raise WebhookSignatureError("no signing secret configured")
    if not header or len(header) > MAX_HEADER_LENGTH:
        raise WebhookSignatureError("missing or oversized signature header")
    timestamp: int | None = None
    candidates: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            raise WebhookSignatureError("malformed signature header")
        if key == "t":
            if timestamp is not None or not value.isdigit() or len(value) > 12:
                raise WebhookSignatureError("malformed signature timestamp")
            timestamp = int(value)
        elif key == SCHEME:
            candidates.append(value)
    if timestamp is None or not candidates:
        raise WebhookSignatureError("the signature header needs t and v1")
    current = int(time.time()) if now is None else int(now)
    if tolerance < 0 or abs(current - timestamp) > tolerance:
        raise WebhookSignatureError("the signature timestamp is outside the tolerance")
    expected = compute_signature(secret, timestamp, payload)
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise WebhookSignatureError("no signature matches")
    return timestamp

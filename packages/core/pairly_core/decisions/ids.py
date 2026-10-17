# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Stable decision ids: ``dec_`` plus a ULID (26 Crockford base32 characters).

The first 10 characters encode the creation time in milliseconds, the other 16 are 80
random bits, so ids sort by time and never repeat in practice. The id is the one a
candidate, a vendor, an auditor or a regulator quotes when they ask "why?".
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Final

PREFIX: Final = "dec_"
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DECISION_ID_RE: Final = re.compile(r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_decision_id(*, now_ms: int | None = None) -> str:
    """A new ``dec_...`` id for the current time (or ``now_ms``)."""
    millis = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= millis < 2**48:
        raise ValueError("timestamp out of range")
    return PREFIX + _encode(millis, 10) + _encode(secrets.randbits(80), 16)


def is_decision_id(value: object) -> bool:
    return isinstance(value, str) and DECISION_ID_RE.fullmatch(value) is not None

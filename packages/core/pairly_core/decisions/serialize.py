# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Exact JSON for decision records.

* ``encode_typed`` / ``decode_typed``: values that must come back with their exact type
  for replay (rule facts): ``Decimal``, ``date``, ``datetime`` and ``float`` become tagged
  objects, so a replay evaluates the very same values. Everything else is plain JSON.
* ``plain_json``: a JSON round trip (tuples become lists, UUIDs strings), so what is
  hashed at write time is exactly what is read back from ``jsonb``.
* ``fingerprint``: a short SHA-256 of a value, used where a value may be personal data
  but a difference still has to be shown (replay diffs).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final

TYPE_KEY: Final = "__pairly_type__"
MAX_DEPTH: Final = 16


class EncodingError(ValueError):
    """A value cannot be stored exactly (unsupported type or too deep)."""


def encode_typed(value: Any, depth: int = 0) -> Any:
    """JSON with type tags for values JSON cannot keep exactly."""
    if depth > MAX_DEPTH:
        raise EncodingError("value nested too deep")
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Enum):
        return encode_typed(value.value, depth + 1)
    if isinstance(value, float):
        return {TYPE_KEY: "float", "v": repr(value)}
    if isinstance(value, Decimal):
        return {TYPE_KEY: "decimal", "v": str(value)}
    if isinstance(value, datetime):
        return {TYPE_KEY: "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {TYPE_KEY: "date", "v": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {TYPE_KEY: "uuid", "v": str(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EncodingError("mapping keys must be strings")
            if key == TYPE_KEY:
                raise EncodingError(f"the key {TYPE_KEY!r} is reserved")
            out[key] = encode_typed(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [encode_typed(item, depth + 1) for item in value]
    raise EncodingError(f"unsupported type {type(value).__name__}")


def decode_typed(value: Any, depth: int = 0) -> Any:
    """The inverse of ``encode_typed``."""
    if depth > MAX_DEPTH:
        raise EncodingError("value nested too deep")
    if isinstance(value, list):
        return [decode_typed(item, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(TYPE_KEY)
    if tag is None:
        return {key: decode_typed(item, depth + 1) for key, item in value.items()}
    raw = value.get("v")
    if not isinstance(raw, str):
        raise EncodingError("tagged value without text")
    match tag:
        case "float":
            return float(raw)
        case "decimal":
            return Decimal(raw)
        case "datetime":
            return datetime.fromisoformat(raw)
        case "date":
            return date.fromisoformat(raw)
        case "uuid":
            return uuid.UUID(raw)
    raise EncodingError(f"unknown type tag {tag!r}")


def _default(value: Any) -> Any:
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def plain_json(value: Any) -> Any:
    """``value`` as it reads back from ``jsonb`` (no NaN or Infinity)."""
    try:
        return json.loads(json.dumps(value, default=_default, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise EncodingError(f"value is not JSON: {exc}") from None


def canonical(value: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: Any) -> str:
    """``sha256:<16 hex>`` of a value's canonical JSON: shows that two values differ
    without storing either of them."""
    try:
        data = canonical(plain_json(value))
    except EncodingError:
        data = repr(value).encode("utf-8", "replace")
    return "sha256:" + sha256_hex(data)[:16]

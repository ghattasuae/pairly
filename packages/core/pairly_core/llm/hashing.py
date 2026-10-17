# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Canonical JSON and the request hash that keys recordings and replay.

``request_hash`` is THE definition of "the same model request":

    sha256( canonical_json({
        "v": 1,
        "task": <task name>,
        "prompt_version": <version string>,
        "prompt_hash": <sha256 of the prompt file bytes>,
        "provider": <provider name>,
        "model": <model id>,
        "inputs": <the caller's inputs, JSON values>,
        "schema_hash": <sha256 of the output schema's canonical JSON schema>,
        "settings": <every request setting that can change the output>,
    }) )

``canonical_json`` sorts keys, drops whitespace, keeps non-ASCII as UTF-8 and refuses
NaN and Infinity, so the same logical request always gives the same bytes. The
organisation is deliberately not part of the hash: recordings are portable between
organisations (seed bundles), and tenant isolation comes from the table's RLS.
Changing anything above (a prompt edit, a schema field, the model, max tokens) gives
a new hash, so a stale recording can never answer a changed request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

REQUEST_HASH_VERSION = 1


def canonical_json(value: Any) -> bytes:
    """UTF-8 JSON with sorted keys and no whitespace. Raises ``ValueError`` on non-JSON."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not plain JSON: {exc}") from None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_round_trip(value: Any) -> Any:
    """A plain-JSON copy of ``value`` (tuples become lists), or ``ValueError``."""
    return json.loads(canonical_json(value))


def schema_json(schema: type[BaseModel]) -> dict[str, Any]:
    result: dict[str, Any] = schema.model_json_schema()
    return result


def schema_hash(schema: type[BaseModel]) -> str:
    return sha256_hex(canonical_json(schema_json(schema)))


def request_hash(
    *,
    task: str,
    prompt_version: str,
    prompt_hash: str,
    provider: str,
    model: str,
    inputs: Mapping[str, Any],
    schema_hash: str,
    settings: Mapping[str, Any],
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "v": REQUEST_HASH_VERSION,
                "task": task,
                "prompt_version": prompt_version,
                "prompt_hash": prompt_hash,
                "provider": provider,
                "model": model,
                "inputs": dict(inputs),
                "schema_hash": schema_hash,
                "settings": dict(settings),
            }
        )
    )

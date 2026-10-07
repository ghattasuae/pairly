# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recording bundles: export stored model responses to a file and import them elsewhere.

Seed data ships a bundle so ``make demo`` runs fully offline in ``recorded`` mode.
A bundle is one JSON document:

    {"format": "pairly.model-recordings", "version": 1,
     "entries": [{request_hash, task, prompt_version, prompt_hash, provider, model, tier,
                  schema_name, schema_hash, request: {inputs, settings}, response,
                  output_complete, stop_reason, status}, ...]}

Import fails closed: every entry's ``request_hash`` is recomputed from its own fields
and the whole bundle is refused if one does not match, so an edited entry can never
answer a different request. Imported rows have ``mode = 'import'`` and cost 0 (the
money was spent elsewhere), so they never count against this org's budget. Bundles
contain the inputs of every call: export only synthetic data (brief hard rule 2).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_core.llm.hashing import canonical_json, request_hash
from pairly_core.llm.store import OUTPUT_STATUSES, seal_call_values
from pairly_core.models.llm import ModelCall

BUNDLE_FORMAT = "pairly.model-recordings"
BUNDLE_VERSION = 1
IMPORT_MODE = "import"
MAX_BUNDLE_ENTRIES = 100_000
MAX_ENTRY_BYTES = 2 * 1024 * 1024

_ENTRY_FIELDS = (
    "request_hash",
    "task",
    "prompt_version",
    "prompt_hash",
    "provider",
    "model",
    "tier",
    "schema_name",
    "schema_hash",
    "request",
    "response",
    "output_complete",
    "stop_reason",
    "status",
)
_STATUSES = frozenset(s.value for s in OUTPUT_STATUSES)


class BundleError(ValueError):
    """The bundle is malformed or an entry does not match its request hash."""


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: int
    skipped: int


async def export_recordings(
    session: AsyncSession, *, task: str | None = None, modes: tuple[str, ...] = ("record", "live")
) -> dict[str, Any]:
    """Newest output per request hash, for the session's organisation (RLS scoped)."""
    query = (
        select(ModelCall)
        .where(
            ModelCall.status.in_(sorted(_STATUSES)),
            ModelCall.mode.in_(modes),
            ModelCall.replay_of.is_(None),
        )
        .order_by(ModelCall.created_at.desc(), ModelCall.id)
    )
    if task is not None:
        query = query.where(ModelCall.task == task)
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for row in (await session.scalars(query)).all():
        if row.request_hash in seen:
            continue
        seen.add(row.request_hash)
        entry = {name: getattr(row, name) for name in _ENTRY_FIELDS}
        entry["request"] = unseal(row, "request")
        entry["response"] = unseal(row, "response")
        entries.append(entry)
    entries.sort(key=lambda e: (e["task"], e["request_hash"]))
    return {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries}


def dumps_bundle(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _check_entry(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != set(_ENTRY_FIELDS):
        raise BundleError(f"entry {index}: wrong fields")
    if len(canonical_json(entry)) > MAX_ENTRY_BYTES:
        raise BundleError(f"entry {index}: too large")
    for name in (
        "request_hash",
        "task",
        "prompt_version",
        "prompt_hash",
        "provider",
        "model",
        "tier",
        "schema_name",
        "schema_hash",
    ):
        if not isinstance(entry[name], str) or not entry[name]:
            raise BundleError(f"entry {index}: {name} must be a non-empty string")
    request = entry["request"]
    if (
        not isinstance(request, dict)
        or set(request) != {"inputs", "settings"}
        or not isinstance(request["inputs"], dict)
        or not isinstance(request["settings"], dict)
    ):
        raise BundleError(f"entry {index}: request must hold inputs and settings objects")
    if entry["status"] not in _STATUSES:
        raise BundleError(f"entry {index}: unknown status")
    if not isinstance(entry["output_complete"], bool):
        raise BundleError(f"entry {index}: output_complete must be a boolean")
    if entry["stop_reason"] is not None and not isinstance(entry["stop_reason"], str):
        raise BundleError(f"entry {index}: stop_reason must be a string or null")
    expected = request_hash(
        task=entry["task"],
        prompt_version=entry["prompt_version"],
        prompt_hash=entry["prompt_hash"],
        provider=entry["provider"],
        model=entry["model"],
        inputs=request["inputs"],
        schema_hash=entry["schema_hash"],
        settings=request["settings"],
    )
    if expected != entry["request_hash"]:
        raise BundleError(f"entry {index}: request_hash does not match its request")
    return entry


def parse_bundle(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError:
        raise BundleError("bundle is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise BundleError("not a Pairly model recording bundle")
    if data.get("version") != BUNDLE_VERSION:
        raise BundleError(f"unsupported bundle version {data.get('version')!r}")
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_BUNDLE_ENTRIES:
        raise BundleError("entries must be a list of at most 100000 items")
    return [_check_entry(e, i) for i, e in enumerate(entries)]


async def import_recordings(
    session: AsyncSession, org_id: uuid.UUID, entries: list[dict[str, Any]]
) -> ImportReport:
    """Insert parsed entries for ``org_id`` (in the caller's transaction). Entries whose
    request hash was already imported are skipped, so importing twice is harmless."""
    existing = set(
        (
            await session.scalars(
                select(ModelCall.request_hash).where(
                    ModelCall.org_id == org_id, ModelCall.mode == IMPORT_MODE
                )
            )
        ).all()
    )
    imported = skipped = 0
    for entry in entries:
        if entry["request_hash"] in existing:
            skipped += 1
            continue
        existing.add(entry["request_hash"])
        values = {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "chain_id": uuid.uuid4(),
            "attempt": 1,
            "mode": IMPORT_MODE,
            "from_recording": True,
            "cost_micros": 0,
            **{name: entry[name] for name in _ENTRY_FIELDS},
        }
        await session.execute(insert(ModelCall).values(**seal_call_values(values)))
        imported += 1
    return ImportReport(imported=imported, skipped=skipped)

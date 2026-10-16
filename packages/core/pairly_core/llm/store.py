# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Storage of model calls and recorded responses (the ``model_calls`` table).

Response bodies live in the database (``model_calls.response``), capped by
``PAIRLY_LLM_MAX_RESPONSE_BYTES``. ``request`` and ``response`` are encrypted with
the row's own context (``pairly_core.crypto``): ``insert`` seals them and
``StoredCall.from_row`` opens them. ``CallStore`` is the seam for moving bodies to object
storage later without touching the router.

A "recording" for a request hash is the newest row with that hash that holds a model
output (status ``ok``, ``invalid_output`` or ``low_confidence``). Replaying it gives the
router exactly what the model said, which it validates again from scratch. In live mode
the router may also reuse the organisation's first live answer to the same request
(``find_reusable``); the new row then names it in ``reused_from``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import seal, unseal
from pairly_core.llm.types import CallStatus
from pairly_core.models.llm import ModelCall

OUTPUT_STATUSES = (CallStatus.OK, CallStatus.INVALID_OUTPUT, CallStatus.LOW_CONFIDENCE)
# Rows that hold a real model response (``recorded`` rows are replays).
REUSABLE_MODES = ("live", "record")
# A response that is not valid JSON is stored under this key, so the raw text can be
# replayed (and rejected again) exactly.
RAW_TEXT_KEY = "__pairly_raw_text__"


@dataclass(frozen=True, slots=True)
class StoredCall:
    id: uuid.UUID
    chain_id: uuid.UUID
    task: str
    prompt_version: str
    prompt_hash: str
    provider: str
    model: str
    tier: str
    schema_name: str
    schema_hash: str
    request_hash: str
    mode: str
    status: str
    request: dict[str, Any]
    response: Any
    output_complete: bool
    stop_reason: str | None
    confidence: float | None

    @classmethod
    def from_row(cls, row: ModelCall) -> StoredCall:
        return cls(
            id=row.id,
            chain_id=row.chain_id,
            task=row.task,
            prompt_version=row.prompt_version,
            prompt_hash=row.prompt_hash,
            provider=row.provider,
            model=row.model,
            tier=row.tier,
            schema_name=row.schema_name,
            schema_hash=row.schema_hash,
            request_hash=row.request_hash,
            mode=row.mode,
            status=row.status,
            request=dict(unseal(row, "request")),
            response=unseal(row, "response"),
            output_complete=row.output_complete,
            stop_reason=row.stop_reason,
            confidence=row.confidence,
        )


def seal_call_values(values: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``values`` (with ``id`` and ``org_id``) with request and response sealed."""
    out = dict(values)
    for column in ("request", "response"):
        if column in out:
            out[column] = seal(
                ModelCall, column, out[column], org_id=out["org_id"], row_id=out["id"]
            )
    return out


@runtime_checkable
class ReusableCallStore(Protocol):
    """A store that can find an earlier live answer to reuse (live mode)."""

    async def find_reusable(
        self, session: AsyncSession, request_hash: str, *, org_id: uuid.UUID, since: datetime
    ) -> StoredCall | None: ...


class CallStore(Protocol):
    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID: ...

    async def find_recording(
        self, session: AsyncSession, request_hash: str
    ) -> StoredCall | None: ...

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None: ...


class DatabaseCallStore:
    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
        call_id = values.setdefault("id", uuid.uuid4())
        assert isinstance(call_id, uuid.UUID)  # noqa: S101
        await session.execute(insert(ModelCall).values(**seal_call_values(values)))
        return call_id

    async def find_recording(self, session: AsyncSession, request_hash: str) -> StoredCall | None:
        row = await session.scalar(
            select(ModelCall)
            .where(
                ModelCall.request_hash == request_hash,
                ModelCall.status.in_([s.value for s in OUTPUT_STATUSES]),
            )
            .order_by(ModelCall.created_at.desc(), ModelCall.id)
            .limit(1)
        )
        return StoredCall.from_row(row) if row is not None else None

    async def find_reusable(
        self, session: AsyncSession, request_hash: str, *, org_id: uuid.UUID, since: datetime
    ) -> StoredCall | None:
        """The organisation's first live answer to ``request_hash`` since ``since``: a
        real model response (live or record mode, not a replay or a reuse itself), not
        erased. The first one, so every later identical request gets the same answer.
        Row Level Security scopes the query to the session's organisation; ``org_id``
        is checked as well."""
        row = await session.scalar(
            select(ModelCall)
            .where(
                ModelCall.org_id == org_id,
                ModelCall.request_hash == request_hash,
                ModelCall.mode.in_(REUSABLE_MODES),
                ModelCall.status.in_([s.value for s in OUTPUT_STATUSES]),
                ModelCall.from_recording.is_(False),
                ModelCall.reused_from.is_(None),
                ModelCall.erased_at.is_(None),
                ModelCall.response.is_not(None),
                ModelCall.created_at >= since,
            )
            .order_by(ModelCall.created_at, ModelCall.id)
            .limit(1)
        )
        return StoredCall.from_row(row) if row is not None else None

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None:
        row = await session.scalar(select(ModelCall).where(ModelCall.id == call_id))
        return StoredCall.from_row(row) if row is not None else None

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Storage of model calls and recorded responses (the ``model_calls`` table).

Response bodies live in the database (``model_calls.response``, jsonb), capped by
``PAIRLY_LLM_MAX_RESPONSE_BYTES``. ``CallStore`` is the seam for moving bodies to object
storage later without touching the router.

A "recording" for a request hash is the newest row with that hash that holds a model
output (status ``ok``, ``invalid_output`` or ``low_confidence``). Replaying it gives the
router exactly what the model said, which it validates again from scratch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.llm.types import CallStatus
from pairly_core.models.llm import ModelCall

OUTPUT_STATUSES = (CallStatus.OK, CallStatus.INVALID_OUTPUT, CallStatus.LOW_CONFIDENCE)
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
            request=dict(row.request),
            response=row.response,
            output_complete=row.output_complete,
            stop_reason=row.stop_reason,
            confidence=row.confidence,
        )


class CallStore(Protocol):
    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID: ...

    async def find_recording(
        self, session: AsyncSession, request_hash: str
    ) -> StoredCall | None: ...

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None: ...


class DatabaseCallStore:
    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
        call_id = values.setdefault("id", uuid.uuid4())
        await session.execute(insert(ModelCall).values(**values))
        assert isinstance(call_id, uuid.UUID)  # noqa: S101
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

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None:
        row = await session.scalar(select(ModelCall).where(ModelCall.id == call_id))
        return StoredCall.from_row(row) if row is not None else None

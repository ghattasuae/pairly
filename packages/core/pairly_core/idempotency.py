# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Stored ``Idempotency-Key`` requests and their responses (WP1.11, migration 0013).

``idempotency_keys`` (org scoped, RLS forced) holds one row per (organisation, actor,
key) for 24 hours: the request hash and, once the request finished, the response
(status, a few headers, the body encrypted like any sensitive field).

The protocol (driven by ``pairly_api.idempotency``):

1. ``claim`` inserts the row in the request's own transaction, before the work. A
   concurrent request with the same key blocks on that insert until the first one
   commits or rolls back (bounded by ``lock_timeout``): a rolled-back first request
   leaves nothing, so the second runs; a committed one makes the second a duplicate.
2. The request runs. Just before its transaction commits, ``set_request_hash`` stores
   the hash of the method, path, query, content type and body. The claim, the hash and
   the business change commit together.
3. After the response, ``store_response`` saves it (a 5xx deletes the row instead, so
   the client may retry).
4. A duplicate compares its own hash: different, 409 ``idempotency_key_reused``; same
   and finished, the stored response is replayed; same and still running, it waits
   briefly, then 409 ``idempotency_request_in_progress``.

Known limits:

* A crash after the commit and before ``store_response`` leaves a row with no response;
  the key then answers 409 until it expires (it never runs the request twice).
* A handler that commits early (the upload route releases its connection while the body
  streams in) commits the claim before the work. A concurrent duplicate then does not
  block on the insert; it waits briefly and answers 409 in progress, so the request still
  never runs twice. If such a handler ends in a 4xx, that response is stored without a
  request hash and is replayed for any request with the same key.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import (
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed, seal, unseal
from pairly_core.db import Base, OrgScoped, TenantError, current_org
from pairly_core.errors import ConflictError

TTL: Final = timedelta(hours=24)
KEY_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_STORED_BODY: Final = 1024 * 1024
LOCK_TIMEOUT: Final = "5s"
REPLAYED_HEADERS: Final = ("content-type", "location")


class IdempotencyKeyReusedError(ConflictError):
    code = "idempotency_key_reused"
    title = "Idempotency key reused with a different request"


class IdempotencyInProgressError(ConflictError):
    code = "idempotency_request_in_progress"
    title = "A request with this idempotency key is still running"


class IdempotencyResponseUnavailableError(ConflictError):
    code = "idempotency_response_unavailable"
    title = "The first response to this idempotency key cannot be replayed"


class IdempotencyRecord(Base, OrgScoped):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "actor_type", "actor_id", "key", name="uq_idempotency_keys_actor_key"
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    # Base64 of the body, encrypted (it may hold anything the endpoint returns).
    response_body: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class Claim:
    """The outcome of ``claim``: a fresh claim (``record_id``) or an existing row."""

    record_id: uuid.UUID
    fresh: bool


def _org(session: AsyncSession) -> uuid.UUID:
    org = current_org(session)
    if org is None:
        raise TenantError("idempotency keys need a tenant context")
    return org


async def claim(session: AsyncSession, *, actor_type: str, actor_id: str, key: str) -> Claim:
    """Claim ``key`` for this actor in the session's transaction (see the module doc)."""
    org = _org(session)
    where = (
        IdempotencyRecord.actor_type == actor_type,
        IdempotencyRecord.actor_id == actor_id,
        IdempotencyRecord.key == key,
    )
    # An expired row no longer counts: remove it so the key can be used again.
    await session.execute(
        delete(IdempotencyRecord)
        .where(*where, IdempotencyRecord.expires_at <= func.now())
        .execution_options(synchronize_session=False)
    )
    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        inserted = await session.scalar(
            insert(IdempotencyRecord)
            .values(
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                key=key,
                expires_at=func.now() + TTL,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_actor_key")
            .returning(IdempotencyRecord.id)
        )
    except DBAPIError as exc:
        # lock_timeout (55P03): the first request is still running. The transaction is
        # aborted; the request fails with 409 and rolls back.
        codes = {getattr(exc.orig, "sqlstate", None), getattr(exc.orig, "pgcode", None)}
        if "55P03" in codes:
            raise IdempotencyInProgressError() from None
        raise
    await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
    if inserted is not None:
        return Claim(record_id=inserted, fresh=True)
    existing = await session.scalar(select(IdempotencyRecord.id).where(*where))
    if existing is None:  # pragma: no cover - deleted in between (expired)
        raise IdempotencyInProgressError()
    return Claim(record_id=existing, fresh=False)


async def load(session: AsyncSession, record_id: uuid.UUID) -> IdempotencyRecord | None:
    return await session.scalar(
        select(IdempotencyRecord)
        .where(IdempotencyRecord.id == record_id)
        .execution_options(populate_existing=True)
    )


def stored_response(record: IdempotencyRecord) -> StoredResponse | None:
    if record.response_status is None:
        return None
    if record.response_body is None:
        raise IdempotencyResponseUnavailableError()
    body = base64.b64decode(unseal(record, "response_body"))
    return StoredResponse(
        status=int(record.response_status), headers=dict(record.response_headers or {}), body=body
    )


async def set_request_hash(session: AsyncSession, record_id: uuid.UUID, digest: str) -> None:
    await session.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.id == record_id)
        .values(request_hash=digest)
        .execution_options(synchronize_session=False)
    )


async def store_response(
    session: AsyncSession,
    record_id: uuid.UUID,
    *,
    status: int,
    headers: dict[str, str],
    body: bytes | None,
) -> bool:
    """Save the response of a finished request (``body`` None: too large to replay).

    A 5xx response deletes the claim instead, so the client can retry. Returns whether a
    row was changed (False when the request's transaction rolled back the claim).
    """
    org = _org(session)
    if status >= 500:
        result: Any = await session.execute(
            delete(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id, IdempotencyRecord.response_status.is_(None))
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)
    values: dict[str, Any] = {
        "response_status": status,
        "response_headers": {k: v for k, v in headers.items() if k in REPLAYED_HEADERS},
        "completed_at": func.now(),
        "response_body": None
        if body is None
        else seal(
            IdempotencyRecord,
            "response_body",
            base64.b64encode(body).decode("ascii"),
            org_id=org,
            row_id=record_id,
        ),
    }
    result = await session.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.id == record_id, IdempotencyRecord.response_status.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def purge_expired(session: AsyncSession, *, limit: int = 5000) -> int:
    """Delete up to ``limit`` expired rows of the session's organisation."""
    _org(session)
    ids = (
        select(IdempotencyRecord.id).where(IdempotencyRecord.expires_at <= func.now()).limit(limit)
    )
    result: Any = await session.execute(
        delete(IdempotencyRecord)
        .where(IdempotencyRecord.id.in_(ids.scalar_subquery()))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)

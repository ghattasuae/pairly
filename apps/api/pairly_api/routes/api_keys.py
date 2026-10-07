# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/api-keys``: create, list and revoke API keys (``core.api_keys.manage``, step-up).

* ``POST /v1/api-keys``                  create; the response holds the full key once
* ``GET  /v1/api-keys``                  list (prefix, scopes, expiry, last use; never secrets)
* ``POST /v1/api-keys/{id}/revoke``      revoke (final)

Scopes may only be read, propose and upload permissions the caller holds. ``mode`` is
``live``, or ``test`` in a sandbox organisation only. Creation does not honour
``Idempotency-Key`` on purpose: a replay would have to store the secret.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pairly_core.api_keys.models import ApiKey
from pairly_core.api_keys.permissions import API_KEYS_MANAGE
from pairly_core.api_keys.service import (
    DEFAULT_DAYS,
    create_api_key,
    list_api_keys,
    revoke_api_key,
)
from pairly_core.api_keys.tokens import KeyMode
from pairly_core.identity import Context, OrgSession
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

ManageActor = Annotated[Actor, Depends(require(API_KEYS_MANAGE))]
Scope = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}\.[a-z]+$")]


class ApiKeyIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    scopes: Annotated[list[Scope], Field(min_length=1, max_length=100)]
    mode: Literal["live", "test"] = "live"
    expires_in_days: Annotated[int, Field(ge=1, le=3650)] = DEFAULT_DAYS
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=100000)] | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    mode: str
    scopes: list[str]
    created_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    rate_limit_per_minute: int | None

    @classmethod
    def of(cls, row: ApiKey) -> ApiKeyOut:
        return cls(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            mode=row.mode,
            scopes=list(row.scopes),
            created_by=row.created_by,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
            rate_limit_per_minute=row.rate_limit_per_minute,
        )


class ApiKeyCreatedOut(ApiKeyOut):
    # The full key. Shown once: store it now, it cannot be read again.
    key: str


@router.post("", status_code=201, response_model=ApiKeyCreatedOut)
async def create_key(
    body: ApiKeyIn, actor: ManageActor, session: OrgSession, ctx: Context
) -> ApiKeyCreatedOut:
    created = await create_api_key(
        session,
        ctx.registry,
        actor,
        name=body.name,
        scopes=body.scopes,
        mode=KeyMode(body.mode),
        expires_in_days=body.expires_in_days,
        max_days=ctx.settings.api_key_max_days,
        rate_limit_per_minute=body.rate_limit_per_minute,
    )
    return ApiKeyCreatedOut(**ApiKeyOut.of(created.key).model_dump(), key=created.token)


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(actor: ManageActor, session: OrgSession) -> list[ApiKeyOut]:
    return [ApiKeyOut.of(row) for row in await list_api_keys(session)]


@router.post("/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_key(key_id: uuid.UUID, actor: ManageActor, session: OrgSession) -> ApiKeyOut:
    return ApiKeyOut.of(await revoke_api_key(session, actor, key_id))

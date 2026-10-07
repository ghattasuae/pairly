# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/flags``: the organisation's feature flags (WP1.12).

* ``GET    /v1/flags``         every declared flag, its default and its value here
  (``core.flags.read``)
* ``PUT    /v1/flags/{key}``   turn a flag on or off (``core.flags.manage``, step-up)
* ``DELETE /v1/flags/{key}``   remove the override: back to the declared default

Unknown keys answer 404. Every change is audited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel

from pairly_core.errors import NotFoundError
from pairly_core.flags.permissions import FLAGS_MANAGE, FLAGS_READ
from pairly_core.flags.registry import FLAG_KEY_RE, flag_registry
from pairly_core.flags.service import EffectiveFlag, effective_flags, reset_flag, set_flag
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/flags", tags=["flags"])

ReadActor = Annotated[Actor, Depends(require(FLAGS_READ))]
ManageActor = Annotated[Actor, Depends(require(FLAGS_MANAGE))]
FlagKey = Annotated[str, Path(min_length=3, max_length=100, pattern=FLAG_KEY_RE.pattern)]


class FlagOut(BaseModel):
    key: str
    description: str
    default: bool
    enabled: bool
    overridden: bool

    @classmethod
    def of(cls, flag: EffectiveFlag) -> FlagOut:
        return cls(
            key=flag.key,
            description=flag.description,
            default=flag.default,
            enabled=flag.enabled,
            overridden=flag.overridden,
        )


class FlagIn(BaseModel):
    enabled: bool


def _known(key: str) -> str:
    if key not in flag_registry():
        raise NotFoundError("unknown feature flag")
    return key


@router.get("", response_model=list[FlagOut])
async def list_flags(actor: ReadActor, session: OrgSession) -> list[FlagOut]:
    return [FlagOut.of(f) for f in await effective_flags(session)]


@router.put("/{key}", response_model=FlagOut)
async def put_flag(key: FlagKey, body: FlagIn, actor: ManageActor, session: OrgSession) -> FlagOut:
    return FlagOut.of(await set_flag(session, actor, _known(key), body.enabled))


@router.delete("/{key}", response_model=FlagOut)
async def delete_flag(key: FlagKey, actor: ManageActor, session: OrgSession) -> FlagOut:
    return FlagOut.of(await reset_flag(session, actor, _known(key)))

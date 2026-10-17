# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``/v1/flags``: the organisation's feature flags (WP1.12).

* ``GET    /v1/flags``         every declared flag, its default and its value here
  (``core.flags.read``)
* ``PUT    /v1/flags/{key}``   turn a flag on or off (``core.flags.manage``, step-up)
* ``DELETE /v1/flags/{key}``   remove the override: back to the declared default

Unknown keys answer 404. Every change is audited.

Turning ``search.documents`` off (by PUT or by a DELETE back to the default) queues the
worker's purge of the organisation's document chunks after the change commits
(``pairly_core.search.documents.PURGE_JOB``). When the queue is unavailable the purge
runs in the request instead, so the opt-out never leaves a searchable copy behind.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel

from pairly_api.decision_services import DecisionServices, get_decision_services
from pairly_core.audit import actor_ref
from pairly_core.errors import NotFoundError
from pairly_core.flags.permissions import FLAGS_MANAGE, FLAGS_READ
from pairly_core.flags.registry import FLAG_KEY_RE, SEARCH_DOCUMENTS, flag_registry
from pairly_core.flags.service import EffectiveFlag, effective_flags, reset_flag, set_flag
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require
from pairly_core.search.documents import PURGE_JOB, purge_documents

router = APIRouter(prefix="/flags", tags=["flags"])

ReadActor = Annotated[Actor, Depends(require(FLAGS_READ))]
ManageActor = Annotated[Actor, Depends(require(FLAGS_MANAGE))]
# Its task queue is the API's general arq queue (not only for decision jobs).
Services = Annotated[DecisionServices, Depends(get_decision_services)]
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


async def _after_change(
    flag: EffectiveFlag, actor: Actor, session: OrgSession, services: DecisionServices
) -> None:
    if flag.key != SEARCH_DOCUMENTS or flag.enabled:
        return
    # After the commit, so the worker sees the flag off (it re-checks before deleting).
    await session.commit()
    queued = await services.queue.enqueue(
        PURGE_JOB, str(actor.org_id), job_id=f"{PURGE_JOB}:{actor.org_id}:{uuid.uuid4().hex}"
    )
    if not queued:
        kind, actor_id = actor_ref(actor)
        await purge_documents(session, actor_type=kind, actor_id=actor_id)


@router.put("/{key}", response_model=FlagOut)
async def put_flag(
    key: FlagKey, body: FlagIn, actor: ManageActor, session: OrgSession, services: Services
) -> FlagOut:
    flag = await set_flag(session, actor, _known(key), body.enabled)
    await _after_change(flag, actor, session, services)
    return FlagOut.of(flag)


@router.delete("/{key}", response_model=FlagOut)
async def delete_flag(
    key: FlagKey, actor: ManageActor, session: OrgSession, services: Services
) -> FlagOut:
    flag = await reset_flag(session, actor, _known(key))
    await _after_change(flag, actor, session, services)
    return FlagOut.of(flag)

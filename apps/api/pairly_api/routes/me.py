# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/me``: the authenticated user, the selected organisation, roles and permissions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.models import Organisation, User

router = APIRouter(tags=["identity"])


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class RoleOut(BaseModel):
    role: str
    workflow: str | None


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    org: OrgOut
    roles: list[RoleOut]
    permissions: list[str]


@router.get("/me", response_model=MeOut)
async def me(actor: CurrentActor, session: OrgSession) -> MeOut:
    """Any member of the organisation may read their own profile; no extra permission."""
    user = await session.get(User, actor.id)
    org = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
    if user is None or org is None:
        raise ForbiddenError("no access to this organisation")
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        org=OrgOut(id=org.id, name=org.name, slug=org.slug),
        roles=[RoleOut(role=g.role, workflow=g.workflow) for g in actor.grants],
        permissions=sorted(actor.permissions),
    )

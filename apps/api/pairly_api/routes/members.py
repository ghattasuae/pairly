# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``GET /v1/members``: people in the organisation and their roles (``core.users.read``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from pairly_core.identity import OrgSession
from pairly_core.models import Membership, Role, User
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/members", tags=["members"])

ReadActor = Annotated[Actor, Depends(require("core.users.read"))]


class MemberRoleOut(BaseModel):
    membership_id: uuid.UUID
    role: str
    workflow: str | None
    granted_at: datetime


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    roles: list[MemberRoleOut]


class MemberList(BaseModel):
    items: list[MemberOut]


@router.get("", response_model=MemberList)
async def list_members(actor: ReadActor, session: OrgSession) -> MemberList:
    """Every member of the current organisation (RLS limits rows to it), by email."""
    rows = (
        await session.execute(
            select(User, Membership.id, Membership.workflow, Membership.created_at, Role.key)
            .join(Membership, Membership.user_id == User.id)
            .join(Role, Role.id == Membership.role_id)
            .where(Membership.org_id == actor.org_id)
            .order_by(User.email, Role.key, Membership.workflow)
        )
    ).all()
    members: dict[uuid.UUID, MemberOut] = {}
    for user, membership_id, workflow, granted_at, role_key in rows:
        member = members.setdefault(
            user.id,
            MemberOut(user_id=user.id, email=user.email, display_name=user.display_name, roles=[]),
        )
        member.roles.append(
            MemberRoleOut(
                membership_id=membership_id, role=role_key, workflow=workflow, granted_at=granted_at
            )
        )
    return MemberList(items=list(members.values()))

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``/v1/me``: the authenticated user, the selected organisation, roles and permissions.

``/v1/me/orgs`` lists the organisations the user belongs to and needs no
``X-Pairly-Org`` header (the web app's organisation switcher uses it).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession, SubjectSession
from pairly_core.invitations import my_organisations
from pairly_core.models import Organisation, User
from pairly_core.rbac import requires_second_factor

router = APIRouter(tags=["identity"])


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class MeOrgOut(OrgOut):
    # A sandbox organisation (``pairly-admin sandbox create``): only test API keys.
    sandbox: bool


class RoleOut(BaseModel):
    role: str
    workflow: str | None


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    org: MeOrgOut
    roles: list[RoleOut]
    permissions: list[str]
    # Whether this sign-in included a TOTP code (JWT amr contains "otp").
    second_factor: bool
    # The held permissions that are refused until the user signs in with TOTP.
    second_factor_permissions: list[str]


class MyOrgsOut(BaseModel):
    items: list[OrgOut]


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
        org=MeOrgOut(id=org.id, name=org.name, slug=org.slug, sandbox=org.sandbox),
        roles=[RoleOut(role=g.role, workflow=g.workflow) for g in actor.grants],
        permissions=sorted(actor.permissions),
        second_factor=actor.has_second_factor,
        second_factor_permissions=sorted(p for p in actor.permissions if requires_second_factor(p)),
    )


@router.get("/me/orgs", response_model=MyOrgsOut)
async def my_orgs(session: SubjectSession) -> MyOrgsOut:
    """Active organisations where the token's subject has a membership. Only those."""
    orgs = await my_organisations(session)
    return MyOrgsOut(items=[OrgOut(id=o.id, name=o.name, slug=o.slug) for o in orgs])

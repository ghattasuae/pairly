# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``/v1/invitations``: invite people, list and revoke invitations, and accept one.

* ``POST   /v1/invitations``          invite (``core.users.manage``; returns the token once)
* ``GET    /v1/invitations``          list (``core.users.read``)
* ``DELETE /v1/invitations/{id}``     revoke a pending invitation (``core.users.manage``)
* ``POST   /v1/invitations/preview``  what a token invites to (token only, no sign-in)
* ``POST   /v1/invitations/accept``   join as the signed-in user (JWT, no org header)

Tokens travel in request bodies only, never in URLs, so they stay out of access logs.
The token and RLS logic lives in ``pairly_core.invitations``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from pairly_core.identity import (
    Context,
    OrgSession,
    RequestSession,
    SubjectSession,
    VerifiedSubject,
)
from pairly_core.invitations import (
    InvitationView,
    accept_invitation,
    create_invitation,
    list_invitations,
    preview_invitation,
    revoke_invitation,
)
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/invitations", tags=["invitations"])

ManageActor = Annotated[Actor, Depends(require("core.users.manage"))]
ReadActor = Annotated[Actor, Depends(require("core.users.read"))]

# token_urlsafe(32) is 43 characters; anything else is rejected before any lookup.
Token = Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")]


class InvitationIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    role: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    workflow: Annotated[
        str | None, Field(default=None, max_length=32, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    ] = None


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    workflow: str | None
    status: Literal["pending", "accepted", "revoked", "expired"]
    created_at: datetime
    expires_at: datetime
    invited_by: uuid.UUID | None


class InvitationCreatedOut(BaseModel):
    invitation: InvitationOut
    # Shown once. The caller emails it to the invitee and must not store or log it.
    token: str


class InvitationList(BaseModel):
    items: list[InvitationOut]


class TokenIn(BaseModel):
    token: Token


class InvitationPreviewOut(BaseModel):
    org_name: str
    email: str
    role: str
    workflow: str | None
    expires_at: datetime


class AcceptedOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: str
    workflow: str | None


def _out(view: InvitationView) -> InvitationOut:
    return InvitationOut(
        id=view.id,
        email=view.email,
        role=view.role,
        workflow=view.workflow,
        status=view.status,
        created_at=view.created_at,
        expires_at=view.expires_at,
        invited_by=view.invited_by,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvitationCreatedOut)
async def create(
    body: InvitationIn, actor: ManageActor, session: OrgSession, ctx: Context
) -> InvitationCreatedOut:
    """Invite a person by email. Refuses the agent role and any role that would give the
    invitee core permissions the inviter does not hold."""
    created = await create_invitation(
        session, ctx.registry, actor, email=body.email, role=body.role, workflow=body.workflow
    )
    return InvitationCreatedOut(invitation=_out(created.invitation), token=created.token)


@router.get("", response_model=InvitationList)
async def list_(actor: ReadActor, session: OrgSession) -> InvitationList:
    del actor
    return InvitationList(items=[_out(v) for v in await list_invitations(session)])


@router.delete("/{invitation_id}", response_model=InvitationOut)
async def revoke(
    invitation_id: uuid.UUID, actor: ManageActor, session: OrgSession
) -> InvitationOut:
    return _out(await revoke_invitation(session, actor, invitation_id))


@router.post("/preview", response_model=InvitationPreviewOut)
async def preview(body: TokenIn, session: RequestSession) -> InvitationPreviewOut:
    """For the invite page before sign-in. Unknown, used, revoked and expired tokens all
    get the same 404."""
    found = await preview_invitation(session, body.token)
    return InvitationPreviewOut(
        org_name=found.org_name,
        email=found.email,
        role=found.role,
        workflow=found.workflow,
        expires_at=found.expires_at,
    )


@router.post("/accept", response_model=AcceptedOut)
async def accept(
    body: TokenIn, verified: VerifiedSubject, session: SubjectSession, ctx: Context
) -> AcceptedOut:
    """Join the organisation of the invitation as the token's subject. The token's
    verified email must equal the invited address."""
    accepted = await accept_invitation(session, ctx.registry, verified, body.token)
    return AcceptedOut(
        org_id=accepted.org_id,
        org_name=accepted.org_name,
        org_slug=accepted.org_slug,
        role=accepted.role,
        workflow=accepted.workflow,
    )

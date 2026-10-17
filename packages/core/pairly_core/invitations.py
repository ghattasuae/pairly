# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Invitation-driven provisioning: the only way a person joins an organisation.

Tokens
    ``secrets.token_urlsafe(32)``: 32 random bytes, 43 URL-safe characters. Shown once (to
    the inviting server, which emails it) and never stored: the database keeps
    ``sha256(token)`` as lowercase hex. An invitation expires 7 days after it is created,
    is bound to one email address and can be used once (accepting or revoking it is
    final, and a database trigger refuses any later change).

Accepting (``accept_invitation``) runs before the user has any membership, so there is
no organisation to put in the tenant context yet. It works in two steps:

1. With no organisation set (tenant policies match nothing), call the SECURITY DEFINER
   function ``invitation_org_id(sha256(token))`` (migration 0003). It returns the
   organisation of a PENDING invitation with exactly that hash, else NULL, and nothing
   else. A forged, used, revoked or expired token gets NULL and the request stops.
2. End that transaction, bind the session to the returned organisation (``set_tenant``),
   and from here on work under normal RLS: lock the invitation row, re-check that it is
   still pending (two concurrent accepts serialise on the lock; the loser sees it used),
   check the verified email, then create the ``users`` row for the token's own subject if
   missing (the users RLS policy allows only that), the membership and the accepted
   mark. All of it commits in one transaction with its audit events, or not at all.

The organisation id is never taken from the client, so a valid token for organisation A
cannot be used to read or join organisation B.

Creating an invitation needs ``core.users.manage`` and passes ``rbac.check_grantable``
(no agent role, no escalation of core permissions). People cannot invite themselves.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import (
    ActorType,
    AuditAction,
    append_audit,
    audit_actor_action,
    record_membership_change,
)
from pairly_core.auth import VerifiedToken
from pairly_core.db import current_org, set_tenant
from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_core.models import Invitation, Membership, Organisation, Role, User
from pairly_core.rbac import (
    Actor,
    ActorKind,
    BuiltinRole,
    RoleGrant,
    RoleRegistry,
    check_grantable,
)

TOKEN_BYTES: Final = 32
INVITATION_TTL: Final = timedelta(days=7)
MAX_EMAIL_LENGTH: Final = 320
# token_urlsafe(32) is exactly 43 characters of the URL-safe base64 alphabet.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# Deliberately simple: one @, something on each side, a dot in the domain, no spaces or
# control characters. The real check is that the person can sign in at that address.
_EMAIL_RE = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

Status = Literal["pending", "accepted", "revoked", "expired"]


# ------------------------------------------------------------------ errors


class InvitationInvalidError(NotFoundError):
    """Unknown, used, revoked or expired. One answer for all, so nothing is revealed."""

    code = "invitation_invalid"
    title = "Invitation not found or no longer valid"


class InvitationEmailMismatchError(ForbiddenError):
    code = "invitation_email_mismatch"
    title = "This invitation was sent to a different email address"


class EmailNotVerifiedError(ForbiddenError):
    code = "email_not_verified"
    title = "Sign in with a verified email address to accept an invitation"


class InvitationPendingError(ConflictError):
    code = "invitation_pending"
    title = "This person already has a pending invitation"


class AlreadyMemberError(ConflictError):
    code = "already_member"
    title = "This person already has this role"


class InvitationClosedError(ConflictError):
    code = "invitation_closed"
    title = "This invitation is no longer pending"


class SelfInvitationError(BadRequestError):
    code = "cannot_invite_self"
    title = "You cannot invite yourself"


# ------------------------------------------------------------------ tokens and values


def new_token() -> tuple[str, str]:
    """A fresh invitation token and its stored hash."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """``sha256(token)`` as lowercase hex: the only form of a token the database sees."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_well_formed_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def normalize_email(value: str) -> str:
    """Trimmed and lower-cased. Raises ``BadRequestError`` if it is not an address."""
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(email):
        raise BadRequestError("invalid email address")
    return email


def invitation_status(invitation: Invitation, now: datetime) -> Status:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.expires_at <= now:
        return "expired"
    return "pending"


async def _db_now(session: AsyncSession) -> datetime:
    """Database time, the same clock the lookup function and constraints use."""
    now = await session.scalar(select(func.now()))
    if not isinstance(now, datetime):
        raise RuntimeError("database returned no time")
    return now


# ------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class InvitationView:
    id: uuid.UUID
    email: str
    role: str
    workflow: str | None
    status: Status
    created_at: datetime
    expires_at: datetime
    invited_by: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    invitation: InvitationView
    # Shown once to the caller, which sends it to the invitee. Never stored or logged.
    token: str


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    org_name: str
    email: str
    role: str
    workflow: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: str
    workflow: str | None
    user_id: uuid.UUID
    membership_created: bool


def _view(invitation: Invitation, role_key: str, now: datetime) -> InvitationView:
    return InvitationView(
        id=invitation.id,
        email=invitation.email,
        role=role_key,
        workflow=invitation.workflow,
        status=invitation_status(invitation, now),
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        invited_by=invitation.invited_by,
    )


# ------------------------------------------------------------------ creating


async def _builtin_role(session: AsyncSession, key: str) -> Role:
    role = await session.scalar(select(Role).where(Role.key == key, Role.org_id.is_(None)))
    if role is None:
        raise BadRequestError("unknown role")
    return role


async def _insert_invitation(
    session: AsyncSession,
    *,
    email: str,
    role: Role,
    workflow: str | None,
    invited_by: uuid.UUID | None,
) -> tuple[Invitation, str]:
    now = await _db_now(session)
    token, token_hash = new_token()
    invitation = Invitation(
        email=email,
        role_id=role.id,
        workflow=workflow,
        token_hash=token_hash,
        invited_by=invited_by,
        created_at=now,
        expires_at=now + INVITATION_TTL,
    )
    session.add(invitation)
    await session.flush()
    return invitation, token


async def create_invitation(
    session: AsyncSession,
    registry: RoleRegistry,
    actor: Actor,
    *,
    email: str,
    role: str,
    workflow: str | None,
) -> CreatedInvitation:
    """Invite ``email`` to the actor's organisation. The session must be bound to it."""
    check_grantable(registry, actor, role, workflow)
    if current_org(session) != actor.org_id:
        raise ForbiddenError("session is not bound to the actor's organisation")
    address = normalize_email(email)
    if actor.email is not None and actor.email.strip().lower() == address:
        raise SelfInvitationError()
    role_row = await _builtin_role(session, role)
    now = await _db_now(session)

    member = await session.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(
            func.lower(User.email) == address,
            Membership.role_id == role_row.id,
            Membership.workflow.is_not_distinct_from(workflow),
        )
        .limit(1)
    )
    if member is not None:
        raise AlreadyMemberError()
    pending = await session.scalar(
        select(Invitation.id)
        .where(
            Invitation.email == address,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .limit(1)
    )
    if pending is not None:
        raise InvitationPendingError()

    invitation, token = await _insert_invitation(
        session, email=address, role=role_row, workflow=workflow, invited_by=actor.id
    )
    await audit_actor_action(
        session,
        actor,
        AuditAction.INVITATION_CREATED,
        entity_type="invitation",
        entity_id=invitation.id,
        workflow=workflow,
        payload={"role": role_row.key, "workflow": workflow},
    )
    return CreatedInvitation(invitation=_view(invitation, role_row.key, now), token=token)


async def list_invitations(session: AsyncSession, *, limit: int = 200) -> list[InvitationView]:
    """The bound organisation's invitations, newest first (RLS limits the rows)."""
    now = await _db_now(session)
    rows = (
        await session.execute(
            select(Invitation, Role.key)
            .join(Role, Role.id == Invitation.role_id)
            .order_by(Invitation.created_at.desc(), Invitation.id)
            .limit(limit)
        )
    ).all()
    return [_view(invitation, key, now) for invitation, key in rows]


async def revoke_invitation(
    session: AsyncSession, actor: Actor, invitation_id: uuid.UUID
) -> InvitationView:
    """Revoke a pending invitation. Another organisation's id is simply not found."""
    row = (
        await session.execute(
            select(Invitation, Role.key)
            .join(Role, Role.id == Invitation.role_id)
            .where(Invitation.id == invitation_id)
            .with_for_update(of=Invitation)
        )
    ).first()
    if row is None:
        raise NotFoundError("invitation not found")
    invitation, role_key = row
    now = await _db_now(session)
    if invitation_status(invitation, now) != "pending":
        raise InvitationClosedError()
    invitation.revoked_at = now
    invitation.revoked_by = actor.id
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        AuditAction.INVITATION_REVOKED,
        entity_type="invitation",
        entity_id=invitation.id,
        workflow=invitation.workflow,
        payload={"role": role_key, "workflow": invitation.workflow},
    )
    return _view(invitation, role_key, now)


# ------------------------------------------------------------------ token holders


async def _bind_to_invitation(session: AsyncSession, token: str, subject: str | None) -> str:
    """Step 1 and the switch to step 2 (see the module docstring). Returns the hash.

    Must start with no organisation in the session's tenant context. Afterwards the
    session is bound to the invitation's organisation and no transaction is open.
    """
    if current_org(session) is not None:
        raise RuntimeError("the token lookup must start without an organisation")
    if not is_well_formed_token(token):
        raise InvitationInvalidError()
    token_hash = hash_token(token)
    org_id = await session.scalar(select(func.invitation_org_id(token_hash)))
    # End the organisation-less transaction: set_tenant applies from the next one.
    await session.rollback()
    if not isinstance(org_id, uuid.UUID):
        raise InvitationInvalidError()
    set_tenant(session, org_id, subject)
    return token_hash


async def _pending_invitation(
    session: AsyncSession, token_hash: str, *, lock: bool
) -> tuple[Invitation, Organisation, Role, datetime]:
    """The pending invitation under normal RLS, with its organisation and role."""
    query = select(Invitation).where(Invitation.token_hash == token_hash)
    invitation = await session.scalar(query.with_for_update() if lock else query)
    now = await _db_now(session)
    # None or no longer pending: the row changed after step 1 (a concurrent accept).
    if invitation is None or invitation_status(invitation, now) != "pending":
        raise InvitationInvalidError()
    org = await session.scalar(select(Organisation).where(Organisation.id == invitation.org_id))
    role = await session.scalar(select(Role).where(Role.id == invitation.role_id))
    if org is None or org.status != "active" or role is None:
        raise InvitationInvalidError()
    return invitation, org, role, now


async def preview_invitation(session: AsyncSession, token: str) -> InvitationPreview:
    """What the invitee sees before accepting. Needs the token, nothing else."""
    token_hash = await _bind_to_invitation(session, token, None)
    invitation, org, role, _ = await _pending_invitation(session, token_hash, lock=False)
    return InvitationPreview(
        org_name=org.name,
        email=invitation.email,
        role=role.key,
        workflow=invitation.workflow,
        expires_at=invitation.expires_at,
    )


async def accept_invitation(
    session: AsyncSession, registry: RoleRegistry, verified: VerifiedToken, token: str
) -> AcceptedInvitation:
    """Join the invitation's organisation as the token's subject. See the module docstring."""
    if verified.email is None or not verified.email_verified:
        raise EmailNotVerifiedError()
    email = normalize_email(verified.email)
    token_hash = await _bind_to_invitation(session, token, verified.subject)
    invitation, org, role, now = await _pending_invitation(session, token_hash, lock=True)
    if email != invitation.email:
        raise InvitationEmailMismatchError()

    # The users RLS policy lets a session create and read only its own subject's row.
    user = await session.scalar(select(User).where(User.auth_subject == verified.subject))
    if user is None:
        user = User(auth_subject=verified.subject, email=email)
        session.add(user)
        await session.flush()

    membership_id = await session.scalar(
        pg_insert(Membership)
        .values(
            org_id=org.id,
            user_id=user.id,
            role_id=role.id,
            workflow=invitation.workflow,
        )
        .on_conflict_do_nothing(constraint="uq_memberships_org_user_role_workflow")
        .returning(Membership.id)
    )
    invitation.accepted_at = now
    invitation.accepted_by = user.id
    await session.flush()

    grants = (RoleGrant(role.key, invitation.workflow),)
    actor = Actor(
        kind=ActorKind.USER,
        id=user.id,
        org_id=org.id,
        grants=grants,
        permissions=registry.resolve(grants),
        subject=verified.subject,
        email=user.email,
        amr=verified.amr,
    )
    await audit_actor_action(
        session,
        actor,
        AuditAction.INVITATION_ACCEPTED,
        entity_type="invitation",
        entity_id=invitation.id,
        workflow=invitation.workflow,
        payload={"role": role.key, "workflow": invitation.workflow},
    )
    if membership_id is not None:
        await record_membership_change(
            session,
            actor,
            action=AuditAction.MEMBERSHIP_GRANTED,
            membership_id=membership_id,
            user_id=user.id,
            role_key=role.key,
            workflow=invitation.workflow,
        )
    return AcceptedInvitation(
        org_id=org.id,
        org_name=org.name,
        org_slug=org.slug,
        role=role.key,
        workflow=invitation.workflow,
        user_id=user.id,
        membership_created=membership_id is not None,
    )


# ------------------------------------------------------------------ organisations


@dataclass(frozen=True, slots=True)
class OrgSummary:
    id: uuid.UUID
    name: str
    slug: str


async def my_organisations(session: AsyncSession) -> list[OrgSummary]:
    """Organisations the session's subject belongs to (SECURITY DEFINER, migration 0003).

    The session must carry the subject and no organisation (``SubjectSession``).
    """
    if current_org(session) is not None:
        raise RuntimeError("my_organisations runs without an organisation context")
    rows = (
        await session.execute(
            select(func.my_organisations().table_valued("org_id", "org_name", "org_slug"))
        )
    ).all()
    return [OrgSummary(id=r.org_id, name=r.org_name, slug=r.org_slug) for r in rows]


@dataclass(frozen=True, slots=True)
class BootstrappedOrg:
    org: OrgSummary
    invitation_id: uuid.UUID
    token: str


async def bootstrap_organisation(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    admin_email: str,
    org_id: uuid.UUID | None = None,
) -> BootstrappedOrg:
    """Create an organisation and its first admin invitation (a system action).

    For the owner database connection (``pairly-admin bootstrap-org``): the application
    role cannot insert organisations. ``session`` must have no open transaction.
    """
    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 200:
        raise BadRequestError("organisation name must be 1 to 200 characters")
    if not _SLUG_RE.fullmatch(slug):
        raise BadRequestError("slug must be 2 to 63 characters: a-z, 0-9 and '-'")
    address = normalize_email(admin_email)
    new_id = org_id or uuid.uuid4()
    set_tenant(session, new_id, None)
    session.add(Organisation(id=new_id, name=clean_name, slug=slug))
    await session.flush()
    role = await _builtin_role(session, BuiltinRole.ADMIN.value)
    invitation, token = await _insert_invitation(
        session, email=address, role=role, workflow=None, invited_by=None
    )
    await append_audit(
        session,
        action=AuditAction.ORG_CREATED,
        actor_type=ActorType.SYSTEM,
        actor_id="pairly-admin",
        entity_type="organisation",
        entity_id=new_id,
        payload={"slug": slug},
    )
    await append_audit(
        session,
        action=AuditAction.INVITATION_CREATED,
        actor_type=ActorType.SYSTEM,
        actor_id="pairly-admin",
        entity_type="invitation",
        entity_id=invitation.id,
        payload={"role": role.key, "workflow": None},
    )
    return BootstrappedOrg(
        org=OrgSummary(id=new_id, name=clean_name, slug=slug),
        invitation_id=invitation.id,
        token=token,
    )


async def invite_as_system(
    session: AsyncSession,
    registry: RoleRegistry,
    *,
    email: str,
    role: str,
    workflow: str | None,
    actor_id: str,
) -> CreatedInvitation | None:
    """Invite ``email`` to the session's organisation as a system action (seed scripts).

    For the owner database connection (``pairly-admin seed``), like
    ``bootstrap_organisation``: no person invites, so ``invited_by`` is NULL and the
    audit event names the system ``actor_id``. Idempotent: returns ``None`` when the
    address already holds this membership or has a pending invitation. The ``agent``
    role is never given to a person, and a workflow scope must name a known workflow.
    """
    org = current_org(session)
    if org is None:
        raise ForbiddenError("invitations need a tenant context")
    if role == BuiltinRole.AGENT.value or role not in registry.roles:
        raise BadRequestError("unknown role")
    if workflow is not None and workflow not in registry.workflows:
        raise BadRequestError("unknown workflow")
    address = normalize_email(email)
    role_row = await _builtin_role(session, role)
    now = await _db_now(session)
    member = await session.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(
            func.lower(User.email) == address,
            Membership.role_id == role_row.id,
            Membership.workflow.is_not_distinct_from(workflow),
        )
        .limit(1)
    )
    pending = await session.scalar(
        select(Invitation.id)
        .where(
            Invitation.email == address,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .limit(1)
    )
    if member is not None or pending is not None:
        return None
    invitation, token = await _insert_invitation(
        session, email=address, role=role_row, workflow=workflow, invited_by=None
    )
    await append_audit(
        session,
        action=AuditAction.INVITATION_CREATED,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        entity_type="invitation",
        entity_id=invitation.id,
        workflow=workflow,
        payload={"role": role_row.key, "workflow": workflow},
    )
    return CreatedInvitation(invitation=_view(invitation, role_row.key, now), token=token)


__all__ = [
    "INVITATION_TTL",
    "AcceptedInvitation",
    "AlreadyMemberError",
    "BootstrappedOrg",
    "CreatedInvitation",
    "EmailNotVerifiedError",
    "InvitationClosedError",
    "InvitationEmailMismatchError",
    "InvitationInvalidError",
    "InvitationPendingError",
    "InvitationPreview",
    "InvitationView",
    "OrgSummary",
    "SelfInvitationError",
    "accept_invitation",
    "bootstrap_organisation",
    "create_invitation",
    "hash_token",
    "invitation_status",
    "invite_as_system",
    "list_invitations",
    "my_organisations",
    "new_token",
    "normalize_email",
    "preview_invitation",
    "revoke_invitation",
]

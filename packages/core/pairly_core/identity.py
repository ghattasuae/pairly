# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""FastAPI dependencies that turn a request into an authenticated, tenant-bound actor.

Per request:

1. Verify the bearer JWT (``Authorization: Bearer ...``). Failure: 401.
2. Read the organisation from the ``X-Pairly-Org`` header. Missing: 403, malformed: 400.
3. Open a session whose transaction runs ``set_config('app.org_id', <org>, true)``. From
   here on RLS only shows that organisation's rows.
4. Load the organisation, the user (by token subject) and the user's memberships in it.
   No active organisation, no user or no membership: 403 with one generic message, so
   the response does not reveal which organisations exist.
5. Resolve effective permissions through the role registry.

Route handlers then use ``OrgSession`` (the same session, still in that transaction) and
``require(permission)``. The transaction commits after the handler returns and before
the response is sent; any error rolls it back.

Two routes work before an organisation is chosen, with ``VerifiedSubject`` (the token
only) and ``SubjectSession`` (a session whose tenant context names the subject and no
organisation, so tenant policies match nothing): ``GET /v1/me/orgs`` and
``POST /v1/invitations/accept``. They reach tenant data only through the narrow
SECURITY DEFINER functions described in ``pairly_core.invitations``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_core.audit import (
    INFO_REQUEST_ID,
    AuditAction,
    audit_actor_action,
    events_in_transaction,
    orm_writes_in_transaction,
    record_denial,
)
from pairly_core.auth import TokenVerifier, VerifiedToken
from pairly_core.config import Settings
from pairly_core.db import current_org, set_tenant
from pairly_core.errors import (
    BadRequestError,
    ForbiddenError,
    ServiceUnavailableError,
    UnauthenticatedError,
)
from pairly_core.models import Membership, Organisation, Role, User
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError, RoleGrant, RoleRegistry

log = structlog.get_logger(__name__)

ORG_HEADER = "X-Pairly-Org"
_NO_ACCESS = "no access to this organisation"


@dataclass(frozen=True, slots=True)
class AppContext:
    """Process-wide services, stored on ``app.state.pairly`` by the API factory."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    verifier: TokenVerifier
    registry: RoleRegistry


def get_context(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "pairly", None)
    if not isinstance(ctx, AppContext):
        raise ServiceUnavailableError("service not initialised")
    return ctx


Context = Annotated[AppContext, Depends(get_context)]


def bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthenticatedError("bearer token required")
    return token.strip()


def org_from_header(request: Request) -> uuid.UUID:
    raw = request.headers.get(ORG_HEADER)
    if raw is None or not raw.strip():
        raise ForbiddenError(f"{ORG_HEADER} header required")
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise BadRequestError(f"{ORG_HEADER} must be an organisation id") from None


async def request_session(request: Request, ctx: Context) -> AsyncIterator[AsyncSession]:
    """A session for this request. No tenant is set, so RLS shows no tenant rows.

    Audit hooks (WP0.5):

    * Audit events written during the request (``pairly_core.audit.append_audit``) share
      this transaction, so they commit atomically with the change they record.
    * Before committing, if the request flushed ORM changes but recorded no audit event,
      a generic ``core.request.write`` event is added (safety net) and a warning is
      logged, so no state change goes unaudited. Reads never write audit events.
    * A ``PermissionDeniedError`` rolls the request back, then the denial is recorded in
      a fresh transaction, so it is kept although the denied work is not.
    """
    async with ctx.sessionmaker() as session:
        request_id = request.scope.get("state", {}).get("request_id")
        if isinstance(request_id, str):
            session.info[INFO_REQUEST_ID] = request_id
        try:
            yield session
        except PermissionDeniedError as exc:
            await session.rollback()
            await _audit_denial(session, exc)
            raise
        except BaseException:
            await session.rollback()
            raise
        else:
            if session.in_transaction():
                await _audit_unrecorded_writes(request, session)
                await session.commit()


async def _audit_denial(session: AsyncSession, exc: PermissionDeniedError) -> None:
    """Record a denial in its own transaction. The request is refused either way."""
    if exc.denial is None or current_org(session) != exc.denial.actor.org_id:
        return
    try:
        async with session.begin():
            await record_denial(session, exc)
    except Exception as err:
        log.error("audit_denial_write_failed", error=type(err).__name__)


async def _audit_unrecorded_writes(request: Request, session: AsyncSession) -> None:
    # Flush first: pending ORM changes would otherwise only be flushed by the commit.
    await session.flush()
    if not orm_writes_in_transaction(session) or events_in_transaction(session) > 0:
        return
    actor = getattr(request.state, "actor", None)
    if not isinstance(actor, Actor):
        return
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    log.warning("unaudited_write", method=request.method, route=path)
    await audit_actor_action(
        session,
        actor,
        AuditAction.REQUEST_WRITE,
        payload={"method": request.method, "route": path if isinstance(path, str) else None},
    )


RequestSession = Annotated[AsyncSession, Depends(request_session, scope="function")]


async def load_actor(
    session: AsyncSession,
    registry: RoleRegistry,
    *,
    subject: str,
    org_id: uuid.UUID,
    amr: frozenset[str] = frozenset(),
) -> Actor:
    """Bind ``session`` to ``org_id`` and load the user's actor there, or raise 403."""
    set_tenant(session, org_id, subject)
    org = await session.scalar(select(Organisation).where(Organisation.id == org_id))
    if org is None or org.status != "active":
        raise ForbiddenError(_NO_ACCESS)
    user = await session.scalar(select(User).where(User.auth_subject == subject))
    if user is None:
        raise ForbiddenError(_NO_ACCESS)
    rows = (
        await session.execute(
            select(Role.key, Membership.workflow)
            .join(Role, Role.id == Membership.role_id)
            .where(Membership.user_id == user.id, Membership.org_id == org_id)
        )
    ).all()
    if not rows:
        raise ForbiddenError(_NO_ACCESS)
    grants = tuple(sorted({RoleGrant(role=r.key, workflow=r.workflow) for r in rows}, key=str))
    return Actor(
        kind=ActorKind.USER,
        id=user.id,
        org_id=org_id,
        grants=grants,
        permissions=registry.resolve(grants),
        subject=subject,
        email=user.email,
        amr=amr,
    )


async def verified_subject(request: Request, ctx: Context) -> VerifiedToken:
    """The verified bearer token, cached on the request (one JWKS check per request)."""
    cached = getattr(request.state, "verified_token", None)
    if isinstance(cached, VerifiedToken):
        return cached
    verified = await ctx.verifier.verify(bearer_token(request))
    request.state.verified_token = verified
    return verified


VerifiedSubject = Annotated[VerifiedToken, Depends(verified_subject)]


async def subject_session(verified: VerifiedSubject, session: RequestSession) -> AsyncSession:
    """The request session with the subject set and no organisation (no tenant rows)."""
    set_tenant(session, None, verified.subject)
    return session


SubjectSession = Annotated[AsyncSession, Depends(subject_session)]


async def current_actor(
    request: Request, ctx: Context, session: RequestSession, verified: VerifiedSubject
) -> Actor:
    org_id = org_from_header(request)
    actor = await load_actor(
        session, ctx.registry, subject=verified.subject, org_id=org_id, amr=verified.amr
    )
    request.state.actor = actor
    structlog.contextvars.bind_contextvars(user_id=str(actor.id), org_id=str(actor.org_id))
    return actor


CurrentActor = Annotated[Actor, Depends(current_actor)]


async def org_session(actor: CurrentActor, session: RequestSession) -> AsyncSession:
    """The request session, bound to the authenticated actor's organisation."""
    del actor  # dependency ordering only: the actor sets the tenant context
    return session


OrgSession = Annotated[AsyncSession, Depends(org_session)]

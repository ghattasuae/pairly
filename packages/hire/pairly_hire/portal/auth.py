# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Candidate portal sign-in: magic links and portal sessions (brief 3.9.8, 6.1, 6.7).

Candidates never get a password and never become staff users. Their identity is a
``hire_candidates`` row; the portal acts for them as ``ActorKind.CANDIDATE``.

Magic link (``pl1``)::

    pl1.<org hex>.<token id hex>.<candidate hex>.<secret>

``secret`` is 32 random bytes (URL-safe base64). The row stores only
``sha256("pairly.hire.portal.login|<org>|<candidate>|<token id>|<secret>")``, so the
token is bound to the organisation, the candidate and its row: changing any part of it
(or the row) makes it fail. Single use (``used_at`` is set in the same transaction that
opens the session, under a row lock), short expiry (``REQUEST_TTL`` for a link asked
from the portal, ``ACKNOWLEDGEMENT_TTL`` for the one in the acknowledgement email). A
garbled, forged, used or expired link reads the same: not valid.

Portal session (``ps1``, same shape, purpose ``session``): issued when a magic link is
used, kept by the web app in an HTTP-only cookie scoped to the portal, sent to the API in
the ``X-Pairly-Portal-Session`` header. It never works as a staff credential (staff
routes read ``Authorization`` and verify a JWT or an API key), and staff credentials
never work on portal routes. Absolute expiry ``SESSION_TTL``; sign-out revokes it.

Every portal request binds the database session to the organisation and the portal
subject ``hire_candidate:<id>`` (``pairly_core.db.set_portal_subject``) before its first
query, so Row Level Security itself limits it to the candidate's own rows (migration
``hire_0004b``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_portal_subject, set_tenant
from pairly_core.errors import NotFoundError, PairlyError
from pairly_core.rbac import Actor, ActorKind
from pairly_hire.models import HireCandidate
from pairly_hire.portal.models import HirePortalSession, HirePortalToken

LOGIN_PREFIX: Final = "pl1"
SESSION_PREFIX: Final = "ps1"
SUBJECT_KIND: Final = "hire_candidate"
REQUEST_TTL: Final = timedelta(minutes=30)
ACKNOWLEDGEMENT_TTL: Final = timedelta(days=3)
SESSION_TTL: Final = timedelta(hours=8)
SEEN_EVERY: Final = timedelta(minutes=5)
MAX_TOKEN: Final = 200
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.candidate"
# What a candidate's portal actor holds (``pairly_core.rbac.candidate_may_hold``).
PORTAL_READ: Final = "hire.portal.read"
APPEAL_CREATE: Final = "hire.appeal.create"
INVALID: Final = "this link is not valid any more: ask for a new one"


class PortalAuthError(PairlyError):
    """No valid portal session: 401, one answer for every reason."""

    status = 401
    code = "portal_session_invalid"
    title = "Sign in to the candidate portal"


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    row_id: uuid.UUID
    candidate_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class PortalIdentity:
    """Who a portal request acts for."""

    org_id: uuid.UUID
    candidate_id: uuid.UUID
    session_id: uuid.UUID

    @property
    def actor_id(self) -> str:
        return str(self.candidate_id)

    def actor(self) -> Actor:
        """The candidate as an actor: only their portal and appeals, never staff rights."""
        return Actor(
            kind=ActorKind.CANDIDATE,
            id=self.candidate_id,
            org_id=self.org_id,
            grants=(),
            permissions=frozenset({PORTAL_READ, APPEAL_CREATE}),
        )


def portal_subject(candidate_id: uuid.UUID) -> str:
    return f"{SUBJECT_KIND}:{candidate_id}"


def _digest(
    purpose: str, org_id: uuid.UUID, candidate_id: uuid.UUID, row_id: uuid.UUID, secret: str
) -> str:
    raw = f"pairly.hire.portal.{purpose}|{org_id}|{candidate_id}|{row_id}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _token(
    prefix: str, org_id: uuid.UUID, row_id: uuid.UUID, candidate_id: uuid.UUID, secret: str
) -> str:
    return f"{prefix}.{org_id.hex}.{row_id.hex}.{candidate_id.hex}.{secret}"


def _uuid(value: str) -> uuid.UUID | None:
    if len(value) != 32 or not all(c in "0123456789abcdef" for c in value):
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def parse_hex_uuid(value: str) -> uuid.UUID | None:
    """A 32 character lowercase hex id (as tokens carry them), or ``None``."""
    return _uuid(value)


def parse(prefix: str, token: str) -> Claim | None:
    """The parts of a ``pl1`` or ``ps1`` token, or ``None`` when it is not one."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != prefix:
        return None
    org_id, row_id, candidate_id = _uuid(parts[1]), _uuid(parts[2]), _uuid(parts[3])
    secret = parts[4]
    if org_id is None or row_id is None or candidate_id is None:
        return None
    if len(secret) != 43 or not all(c.isalnum() or c in "-_" for c in secret):
        return None
    return Claim(org_id, row_id, candidate_id, secret)


def bind(session: AsyncSession, org_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
    """Bind a fresh session to the organisation and the candidate (RLS, both layers)."""
    set_tenant(session, org_id, None)
    set_portal_subject(session, portal_subject(candidate_id))


# ------------------------------------------------------------------ magic links


async def issue_login_token(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    candidate_id: uuid.UUID,
    locale: str,
    purpose: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """A new magic link for ``candidate_id`` (the session is bound to ``org_id``)."""
    now = now or datetime.now(UTC)
    row_id = uuid.uuid4()
    secret = _secret()
    expires = now + ttl
    session.add(
        HirePortalToken(
            id=row_id,
            org_id=org_id,
            candidate_id=candidate_id,
            secret_hash=_digest("login", org_id, candidate_id, row_id, secret),
            purpose=purpose,
            locale=locale if locale in {"en", "ar"} else "en",
            created_at=now,
            expires_at=expires,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="hire.portal.link_issued",
        actor_type=ActorType.SYSTEM,
        actor_id="pairly.hire.portal",
        entity_type=ENTITY,
        entity_id=candidate_id,
        workflow=WORKFLOW,
        payload={"token_id": str(row_id), "purpose": purpose, "expires_at": expires.isoformat()},
    )
    return _token(LOGIN_PREFIX, org_id, row_id, candidate_id, secret), expires


@dataclass(frozen=True, slots=True)
class OpenedSession:
    token: str
    identity: PortalIdentity
    expires_at: datetime
    locale: str


async def open_session(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> OpenedSession:
    """Use a magic link once and open a portal session. The database session must be
    fresh (no transaction yet): this binds it to the token's organisation and candidate.
    Raises ``NotFoundError`` (one answer) for any link that is not valid."""
    claim = parse(LOGIN_PREFIX, token)
    if claim is None:
        raise NotFoundError(INVALID)
    bind(session, claim.org_id, claim.candidate_id)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(HirePortalToken).where(HirePortalToken.id == claim.row_id).with_for_update()
    )
    expected = _digest("login", claim.org_id, claim.candidate_id, claim.row_id, claim.secret)
    if (
        row is None
        or row.candidate_id != claim.candidate_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.used_at is not None
        or row.expires_at <= now
    ):
        raise NotFoundError(INVALID)
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == claim.candidate_id)
    )
    if candidate is None:
        raise NotFoundError(INVALID)
    row.used_at = now
    session_id = uuid.uuid4()
    secret = _secret()
    expires = now + SESSION_TTL
    session.add(
        HirePortalSession(
            id=session_id,
            org_id=claim.org_id,
            candidate_id=claim.candidate_id,
            secret_hash=_digest("session", claim.org_id, claim.candidate_id, session_id, secret),
            token_id=row.id,
            created_at=now,
            expires_at=expires,
            last_seen_at=now,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="hire.portal.signed_in",
        actor_type=ActorType.CANDIDATE,
        actor_id=str(claim.candidate_id),
        entity_type=ENTITY,
        entity_id=claim.candidate_id,
        workflow=WORKFLOW,
        payload={"token_id": str(row.id), "portal_session": str(session_id)},
    )
    identity = PortalIdentity(claim.org_id, claim.candidate_id, session_id)
    return OpenedSession(
        _token(SESSION_PREFIX, claim.org_id, session_id, claim.candidate_id, secret),
        identity,
        expires,
        candidate.locale,
    )


# ------------------------------------------------------------------ sessions


async def authenticate(
    session: AsyncSession, token: str | None, *, now: datetime | None = None
) -> PortalIdentity:
    """The identity behind a portal session token, with the database session bound to
    it. Raises ``PortalAuthError`` for anything else."""
    claim = parse(SESSION_PREFIX, token or "")
    if claim is None:
        raise PortalAuthError()
    bind(session, claim.org_id, claim.candidate_id)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(HirePortalSession).where(HirePortalSession.id == claim.row_id)
    )
    expected = _digest("session", claim.org_id, claim.candidate_id, claim.row_id, claim.secret)
    if (
        row is None
        or row.candidate_id != claim.candidate_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.revoked_at is not None
        or row.expires_at <= now
    ):
        raise PortalAuthError()
    if row.last_seen_at is None or now - row.last_seen_at > SEEN_EVERY:
        row.last_seen_at = now
        await session.flush()
    return PortalIdentity(claim.org_id, claim.candidate_id, claim.row_id)


async def sign_out(session: AsyncSession, identity: PortalIdentity) -> None:
    row = await session.scalar(
        select(HirePortalSession)
        .where(HirePortalSession.id == identity.session_id)
        .with_for_update()
    )
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = datetime.now(UTC)
    await session.flush()
    await append_audit(
        session,
        action="hire.portal.signed_out",
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
        entity_type=ENTITY,
        entity_id=identity.candidate_id,
        workflow=WORKFLOW,
        payload={"portal_session": str(identity.session_id)},
    )

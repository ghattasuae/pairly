# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The referee's signed link and session (brief 5.1.6: no login beyond a signed link).

Links (``hire_reference_links``)::

    rr1.<org hex>.<link id hex>.<request hex>.<secret>

``secret`` is 32 random bytes (URL-safe base64). The row stores only
``sha256("pairly.hire.reference.<purpose>|<org>|<request>|<link id>|<secret>")``, so a
link is bound to its organisation, its reference request (one referee, one application)
and its row: changing any part makes it fail. Single use (``used_at`` set under a row
lock), it expires ``LINK_TTL`` (72 hours) after it was sent, and works only while the
request is open (not submitted, not cancelled). A garbled, forged, used or expired link
reads the same: not valid.

Using it starts a referee session (``rs1``, same shape) for that one request,
``SESSION_TTL`` (24 hours): the web app keeps it in an HTTP-only cookie and sends it in
``X-Pairly-Reference-Session``. Progress is saved on the request, so a new link sent by
the hiring team resumes the same draft. Never a staff credential.
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
from pairly_core.db import set_tenant
from pairly_core.errors import NotFoundError, PairlyError
from pairly_hire.references.models import (
    OPEN_STATUSES,
    HireReferenceLink,
    HireReferenceRequest,
    HireReferenceSession,
)

LINK_PREFIX: Final = "rr1"
SESSION_PREFIX: Final = "rs1"
LINK_TTL: Final = timedelta(hours=72)
SESSION_TTL: Final = timedelta(hours=24)
MAX_TOKEN: Final = 200
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.reference"
INVALID: Final = "this link is not valid any more"
REFEREE_ACTOR: Final = "hire_referee"


class RefereeAuthError(PairlyError):
    """No valid referee session: 401, one answer for every reason."""

    status = 401
    code = "reference_session_invalid"
    title = "Open the reference form from its link"


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    row_id: uuid.UUID
    request_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class RefereeIdentity:
    org_id: uuid.UUID
    request_id: uuid.UUID
    session_id: uuid.UUID

    @property
    def actor_id(self) -> str:
        return f"{REFEREE_ACTOR}:{self.request_id}"


def _digest(purpose: str, org: uuid.UUID, request: uuid.UUID, row: uuid.UUID, secret: str) -> str:
    raw = f"pairly.hire.reference.{purpose}|{org.hex}|{request.hex}|{row.hex}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _token(prefix: str, org: uuid.UUID, row: uuid.UUID, request: uuid.UUID, secret: str) -> str:
    return f"{prefix}.{org.hex}.{row.hex}.{request.hex}.{secret}"


def _uuid(value: str) -> uuid.UUID | None:
    if len(value) != 32:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def parse(prefix: str, token: str) -> Claim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != prefix:
        return None
    org, row, request = _uuid(parts[1]), _uuid(parts[2]), _uuid(parts[3])
    secret = parts[4]
    if org is None or row is None or request is None or len(secret) != 43:
        return None
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in secret):
        return None
    return Claim(org, row, request, secret)


async def issue_link(
    session: AsyncSession, request: HireReferenceRequest, *, now: datetime | None = None
) -> tuple[str, datetime]:
    """A new single use link for the referee of ``request``. Earlier unused links of the
    request stop working (one live link at a time)."""
    now = now or datetime.now(UTC)
    for old in await session.scalars(
        select(HireReferenceLink)
        .where(HireReferenceLink.request_id == request.id, HireReferenceLink.used_at.is_(None))
        .with_for_update()
    ):
        old.used_at = now
    row_id = uuid.uuid4()
    secret = _secret()
    expires = now + LINK_TTL
    session.add(
        HireReferenceLink(
            id=row_id,
            org_id=request.org_id,
            request_id=request.id,
            secret_hash=_digest("link", request.org_id, request.id, row_id, secret),
            expires_at=expires,
            created_at=now,
        )
    )
    await session.flush()
    return _token(LINK_PREFIX, request.org_id, row_id, request.id, secret), expires


async def _open_request(session: AsyncSession, request_id: uuid.UUID) -> HireReferenceRequest:
    row = await session.scalar(
        select(HireReferenceRequest).where(HireReferenceRequest.id == request_id).with_for_update()
    )
    if row is None or row.status not in OPEN_STATUSES or row.erased_at is not None:
        raise NotFoundError(INVALID)
    return row


@dataclass(frozen=True, slots=True)
class OpenedSession:
    token: str
    identity: RefereeIdentity
    expires_at: datetime


async def open_session(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> OpenedSession:
    """Use a link once and start a referee session. The database session must be fresh:
    this binds it to the link's organisation. ``NotFoundError`` for any invalid link."""
    claim = parse(LINK_PREFIX, token)
    if claim is None:
        raise NotFoundError(INVALID)
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    link = await session.scalar(
        select(HireReferenceLink).where(HireReferenceLink.id == claim.row_id).with_for_update()
    )
    expected = _digest("link", claim.org_id, claim.request_id, claim.row_id, claim.secret)
    if (
        link is None
        or link.request_id != claim.request_id
        or not hmac.compare_digest(expected, link.secret_hash)
        or link.used_at is not None
        or link.expires_at <= now
    ):
        raise NotFoundError(INVALID)
    request = await _open_request(session, link.request_id)
    link.used_at = now
    session_id = uuid.uuid4()
    secret = _secret()
    expires = now + SESSION_TTL
    session.add(
        HireReferenceSession(
            id=session_id,
            org_id=link.org_id,
            request_id=request.id,
            link_id=link.id,
            secret_hash=_digest("session", link.org_id, request.id, session_id, secret),
            expires_at=expires,
            created_at=now,
        )
    )
    if request.opened_at is None:
        request.opened_at = now
    await session.flush()
    identity = RefereeIdentity(link.org_id, request.id, session_id)
    await append_audit(
        session,
        action="hire.reference.link_used",
        actor_type=ActorType.CANDIDATE,  # an external person (the referee)
        actor_id=identity.actor_id,
        entity_type=ENTITY,
        entity_id=request.id,
        workflow=WORKFLOW,
        payload={"link_id": str(link.id), "reference_session": str(session_id)},
    )
    return OpenedSession(
        _token(SESSION_PREFIX, link.org_id, session_id, request.id, secret), identity, expires
    )


async def authenticate(
    session: AsyncSession, token: str | None, *, now: datetime | None = None
) -> RefereeIdentity:
    """The reference request behind a referee session, with the database session bound to
    its organisation. ``RefereeAuthError`` for anything else (also once the reference
    was submitted or cancelled)."""
    claim = parse(SESSION_PREFIX, token or "")
    if claim is None:
        raise RefereeAuthError()
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(HireReferenceSession).where(HireReferenceSession.id == claim.row_id)
    )
    expected = _digest("session", claim.org_id, claim.request_id, claim.row_id, claim.secret)
    if (
        row is None
        or row.request_id != claim.request_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.revoked_at is not None
        or row.expires_at <= now
    ):
        raise RefereeAuthError()
    request = await session.scalar(
        select(HireReferenceRequest.status).where(HireReferenceRequest.id == row.request_id)
    )
    if request not in OPEN_STATUSES:
        raise RefereeAuthError()
    return RefereeIdentity(claim.org_id, row.request_id, row.id)

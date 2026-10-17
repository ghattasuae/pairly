# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The interviewer's one-click scorecard link (brief 5.3: "a one-click link to the
scorecard, mobile friendly, no login wall beyond a magic link").

Links (``hire_scorecard_links``)::

    sk1.<org hex>.<link id hex>.<user hex>.<secret>   open my scorecard for this interview
    sl1.<org hex>.<link id hex>.<user hex>.<secret>   remind me later (snooze reminders)

``secret`` is 32 random bytes (URL-safe base64). The row stores only
``sha256("pairly.hire.scorecard.<purpose>|<org>|<user>|<link id>|<secret>")``, so a link
is bound to its organisation, its interviewer and its row (which names the interview):
changing any part makes it fail. Single use: ``used_at`` is set under a row lock in the
transaction that acts on it. It expires 7 days after the interview ends. A garbled,
forged, used or expired link reads the same: not valid.

Using an ``open`` link starts a scorecard session (``ss1``, same shape) for that one
interview and that interviewer, 12 hours. The web app keeps it in an HTTP-only cookie
and sends it in ``X-Pairly-Scorecard-Session``. It is never a staff credential: it opens
only that interviewer's scorecard and kit for that interview, and every request checks
the person is still a member and still on the panel. That is the one login step: the
link itself (the page posts it once; email scanners do not run scripts).
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
from pairly_core.models import Membership
from pairly_hire.interviewing.models import HireScorecardLink, HireScorecardSession
from pairly_hire.scheduling.models import HireInterview, HireInterviewPanelist

OPEN_PREFIX: Final = "sk1"
LATER_PREFIX: Final = "sl1"
SESSION_PREFIX: Final = "ss1"
LINK_GRACE: Final = timedelta(days=7)
MIN_LINK_TTL: Final = timedelta(days=2)
SESSION_TTL: Final = timedelta(hours=12)
MAX_TOKEN: Final = 200
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.interview"
INVALID: Final = "this link is not valid any more"
_PURPOSES: Final = {OPEN_PREFIX: "open", LATER_PREFIX: "later", SESSION_PREFIX: "session"}


class ScorecardAuthError(PairlyError):
    """No valid scorecard session: 401, one answer for every reason."""

    status = 401
    code = "scorecard_session_invalid"
    title = "Open your scorecard from its link"


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    row_id: uuid.UUID
    user_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class ScorecardIdentity:
    org_id: uuid.UUID
    user_id: uuid.UUID
    interview_id: uuid.UUID
    session_id: uuid.UUID


def _digest(purpose: str, org: uuid.UUID, user: uuid.UUID, row: uuid.UUID, secret: str) -> str:
    raw = f"pairly.hire.scorecard.{purpose}|{org.hex}|{user.hex}|{row.hex}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _token(prefix: str, org: uuid.UUID, row: uuid.UUID, user: uuid.UUID, secret: str) -> str:
    return f"{prefix}.{org.hex}.{row.hex}.{user.hex}.{secret}"


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
    org, row, user = _uuid(parts[1]), _uuid(parts[2]), _uuid(parts[3])
    secret = parts[4]
    if org is None or row is None or user is None or len(secret) != 43:
        return None
    if not all(c.isalnum() or c in "-_" for c in secret):
        return None
    return Claim(org, row, user, secret)


def org_of(token: str) -> uuid.UUID | None:
    """The organisation a link names (to bind a fresh database session), if well formed."""
    for prefix in _PURPOSES:
        claim = parse(prefix, token)
        if claim is not None:
            return claim.org_id
    return None


async def active_panelist(
    session: AsyncSession, interview_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """The person is still a member of the organisation and still on the panel."""
    seat = await session.scalar(
        select(HireInterviewPanelist.id).where(
            HireInterviewPanelist.interview_id == interview_id,
            HireInterviewPanelist.user_id == user_id,
            HireInterviewPanelist.removed_at.is_(None),
        )
    )
    if seat is None:
        return False
    member = await session.scalar(
        select(Membership.id).where(Membership.user_id == user_id).limit(1)
    )
    return member is not None


async def issue_link(
    session: AsyncSession,
    interview: HireInterview,
    user_id: uuid.UUID,
    *,
    purpose: str = "open",
    now: datetime | None = None,
) -> str:
    """A new single use link for ``user_id`` to their scorecard of ``interview``."""
    now = now or datetime.now(UTC)
    row_id = uuid.uuid4()
    secret = _secret()
    end = interview.ends_at or now
    expires = max(end + LINK_GRACE, now + MIN_LINK_TTL)
    prefix = OPEN_PREFIX if purpose == "open" else LATER_PREFIX
    session.add(
        HireScorecardLink(
            id=row_id,
            org_id=interview.org_id,
            interview_id=interview.id,
            user_id=user_id,
            purpose=purpose,
            secret_hash=_digest(purpose, interview.org_id, user_id, row_id, secret),
            expires_at=expires,
            created_at=now,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="hire.scorecard.link_issued",
        actor_type=ActorType.SYSTEM,
        actor_id="pairly.hire.scorecards",
        entity_type=ENTITY,
        entity_id=interview.id,
        workflow=WORKFLOW,
        payload={
            "link_id": str(row_id),
            "user_id": str(user_id),
            "purpose": purpose,
            "expires_at": expires.isoformat(),
        },
    )
    return _token(prefix, interview.org_id, row_id, user_id, secret)


async def use_link(
    session: AsyncSession, prefix: str, token: str, *, now: datetime | None = None
) -> HireScorecardLink:
    """Use a link once. The database session must be fresh: this binds it to the link's
    organisation. Raises ``NotFoundError`` (one answer) for any link that is not valid."""
    claim = parse(prefix, token)
    if claim is None:
        raise NotFoundError(INVALID)
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    purpose = _PURPOSES[prefix]
    row = await session.scalar(
        select(HireScorecardLink).where(HireScorecardLink.id == claim.row_id).with_for_update()
    )
    expected = _digest(purpose, claim.org_id, claim.user_id, claim.row_id, claim.secret)
    if (
        row is None
        or row.user_id != claim.user_id
        or row.purpose != purpose
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.used_at is not None
        or row.expires_at <= now
        or not await active_panelist(session, row.interview_id, row.user_id)
    ):
        raise NotFoundError(INVALID)
    row.used_at = now
    await session.flush()
    return row


@dataclass(frozen=True, slots=True)
class OpenedSession:
    token: str
    identity: ScorecardIdentity
    expires_at: datetime


async def open_session(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> OpenedSession:
    """Use an ``open`` link and start a scorecard session for its interview."""
    now = now or datetime.now(UTC)
    link = await use_link(session, OPEN_PREFIX, token, now=now)
    session_id = uuid.uuid4()
    secret = _secret()
    expires = now + SESSION_TTL
    session.add(
        HireScorecardSession(
            id=session_id,
            org_id=link.org_id,
            interview_id=link.interview_id,
            user_id=link.user_id,
            link_id=link.id,
            secret_hash=_digest("session", link.org_id, link.user_id, session_id, secret),
            expires_at=expires,
            created_at=now,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="hire.scorecard.link_used",
        actor_type=ActorType.USER,
        actor_id=str(link.user_id),
        entity_type=ENTITY,
        entity_id=link.interview_id,
        workflow=WORKFLOW,
        payload={"link_id": str(link.id), "scorecard_session": str(session_id)},
    )
    identity = ScorecardIdentity(link.org_id, link.user_id, link.interview_id, session_id)
    return OpenedSession(
        _token(SESSION_PREFIX, link.org_id, session_id, link.user_id, secret), identity, expires
    )


async def authenticate(
    session: AsyncSession, token: str | None, *, now: datetime | None = None
) -> ScorecardIdentity:
    """The interviewer and interview behind a scorecard session, with the database
    session bound to the organisation. ``ScorecardAuthError`` for anything else."""
    claim = parse(SESSION_PREFIX, token or "")
    if claim is None:
        raise ScorecardAuthError()
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(HireScorecardSession).where(HireScorecardSession.id == claim.row_id)
    )
    expected = _digest("session", claim.org_id, claim.user_id, claim.row_id, claim.secret)
    if (
        row is None
        or row.user_id != claim.user_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.revoked_at is not None
        or row.expires_at <= now
        or not await active_panelist(session, row.interview_id, row.user_id)
    ):
        raise ScorecardAuthError()
    return ScorecardIdentity(claim.org_id, claim.user_id, row.interview_id, row.id)

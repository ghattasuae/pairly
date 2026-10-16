# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor onboarding by invite link (brief 4.1.1): the signed link and the session.

Links (``pay_vendor_invites``)::

    vi1.<org hex>.<invite id hex>.<secret>

``secret`` is 32 random bytes (URL-safe base64). The row stores only
``sha256("pairly.pay.invite.<purpose>|<org>|<invite id>|<secret>")``, so a link is
bound to its organisation and its row: changing any part makes it fail. It is single
use (``used_at`` set under a row lock), expires ``INVITE_TTL`` (7 days) after it was
made, and dies when revoked. A garbled, forged, used, revoked or expired link reads the
same: not valid.

Using it starts an onboarding session (``vs1.<org>.<session>.<secret>``, same shape),
``SESSION_TTL`` (24 hours), for that one invite: the vendor uploads documents and
submits the form with it (header ``X-Pairly-Vendor-Session``). Submitting ends the
session. Never a staff credential: the session can only add documents and submit.

The submission becomes a ``pending`` vendor (``VendorSource.ONBOARDING``) with its bank
details as a ``proposed`` account and the duplicate hints in ``review_reasons``. An AP
reviewer then accepts it, which puts the bank details through a change ticket
(``pairly_pay.bank_changes``: call back, dual approval), or rejects it.
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

from pairly_core.crypto.types import seal
from pairly_core.db import set_tenant
from pairly_core.errors import NotFoundError, PairlyError
from pairly_pay.common import INVITE_ENTITY, By, audit, opened
from pairly_pay.models import PayVendorInvite, PayVendorOnboardingSession

INVITE_PREFIX: Final = "vi1"
SESSION_PREFIX: Final = "vs1"
INVITE_TTL: Final = timedelta(days=7)
SESSION_TTL: Final = timedelta(hours=24)
MAX_TOKEN: Final = 200
MAX_DOCUMENTS: Final = 10
INVALID: Final = "this link is not valid any more"


class VendorSessionError(PairlyError):
    """No valid onboarding session: 401, one answer for every reason."""

    status = 401
    code = "vendor_session_invalid"
    title = "Open the onboarding form from its link"


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    row_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class VendorIdentity:
    org_id: uuid.UUID
    invite_id: uuid.UUID
    session_id: uuid.UUID

    @property
    def by(self) -> By:
        return By.vendor(self.invite_id)


def _digest(purpose: str, org: uuid.UUID, row: uuid.UUID, secret: str) -> str:
    raw = f"pairly.pay.invite.{purpose}|{org.hex}|{row.hex}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _token(prefix: str, org: uuid.UUID, row: uuid.UUID, secret: str) -> str:
    return f"{prefix}.{org.hex}.{row.hex}.{secret}"


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
    if len(parts) != 4 or parts[0] != prefix:
        return None
    org, row, secret = _uuid(parts[1]), _uuid(parts[2]), parts[3]
    if org is None or row is None or len(secret) != 43:
        return None
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in secret):
        return None
    return Claim(org, row, secret)


def invite_state(invite: PayVendorInvite, now: datetime) -> str:
    if invite.revoked_at is not None:
        return "revoked"
    if invite.vendor_id is not None:
        return "submitted"
    if invite.used_at is not None:
        return "opened"
    if invite.expires_at <= now:
        return "expired"
    return "open"


@dataclass(frozen=True, slots=True)
class IssuedInvite:
    invite: PayVendorInvite
    token: str


async def create_invite(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    by: By,
    company_hint: str | None,
    email: str | None,
    locale: str,
) -> IssuedInvite:
    now = datetime.now(UTC)
    invite_id = uuid.uuid4()
    secret = _secret()
    invite = PayVendorInvite(
        id=invite_id,
        org_id=org_id,
        company_hint=company_hint,
        email=seal(PayVendorInvite, "email", email, org_id=org_id, row_id=invite_id)
        if email
        else None,
        locale=locale,
        secret_hash=_digest("link", org_id, invite_id, secret),
        expires_at=now + INVITE_TTL,
        created_by=created_by,
        created_at=now,
    )
    session.add(invite)
    await session.flush()
    await audit(
        session,
        by,
        "pay.vendor_invite.created",
        entity_type=INVITE_ENTITY,
        entity_id=invite_id,
        payload={"expires_at": invite.expires_at.isoformat(), "has_email": email is not None},
    )
    return IssuedInvite(invite, _token(INVITE_PREFIX, org_id, invite_id, secret))


def invite_email(invite: PayVendorInvite) -> str | None:
    value = opened(invite, "email")
    return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class OpenedSession:
    token: str
    identity: VendorIdentity
    expires_at: datetime


async def open_session(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> OpenedSession:
    """Use an invite link once and start an onboarding session. The database session
    must be fresh: this binds it to the link's organisation."""
    claim = parse(INVITE_PREFIX, token)
    if claim is None:
        raise NotFoundError(INVALID)
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    invite = await session.scalar(
        select(PayVendorInvite).where(PayVendorInvite.id == claim.row_id).with_for_update()
    )
    expected = _digest("link", claim.org_id, claim.row_id, claim.secret)
    if (
        invite is None
        or not hmac.compare_digest(expected, invite.secret_hash)
        or invite_state(invite, now) != "open"
    ):
        raise NotFoundError(INVALID)
    invite.used_at = now
    session_id = uuid.uuid4()
    secret = _secret()
    expires = now + SESSION_TTL
    session.add(
        PayVendorOnboardingSession(
            id=session_id,
            org_id=invite.org_id,
            invite_id=invite.id,
            secret_hash=_digest("session", invite.org_id, session_id, secret),
            expires_at=expires,
            document_ids=[],
            created_at=now,
        )
    )
    await session.flush()
    identity = VendorIdentity(invite.org_id, invite.id, session_id)
    await audit(
        session,
        identity.by,
        "pay.vendor_invite.opened",
        entity_type=INVITE_ENTITY,
        entity_id=invite.id,
        payload={"onboarding_session": str(session_id)},
    )
    return OpenedSession(
        _token(SESSION_PREFIX, invite.org_id, session_id, secret), identity, expires
    )


async def authenticate(
    session: AsyncSession, token: str | None, *, now: datetime | None = None
) -> tuple[VendorIdentity, PayVendorOnboardingSession]:
    """The onboarding session behind a token, with the database session bound to its
    organisation. ``VendorSessionError`` for anything else (also once submitted)."""
    claim = parse(SESSION_PREFIX, token or "")
    if claim is None:
        raise VendorSessionError()
    set_tenant(session, claim.org_id, None)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(PayVendorOnboardingSession)
        .where(PayVendorOnboardingSession.id == claim.row_id)
        .with_for_update()
    )
    expected = _digest("session", claim.org_id, claim.row_id, claim.secret)
    if (
        row is None
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.submitted_at is not None
        or row.expires_at <= now
    ):
        raise VendorSessionError()
    invite = await session.scalar(
        select(PayVendorInvite).where(PayVendorInvite.id == row.invite_id)
    )
    if invite is None or invite.revoked_at is not None or invite.vendor_id is not None:
        raise VendorSessionError()
    return VendorIdentity(claim.org_id, row.invite_id, row.id), row

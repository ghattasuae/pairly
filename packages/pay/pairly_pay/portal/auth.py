# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Vendor portal sign-in: magic links and portal sessions (brief 4.7).

A vendor's contacts never get a password and never become staff users. Their identity
is a ``pay_portal_contacts`` row that the AP team added from the vendor's contacts on
file; the portal acts for them as ``ActorKind.VENDOR``.

Magic link (``vl1``)::

    vl1.<org hex>.<token id hex>.<vendor hex>.<contact hex>.<secret>

``secret`` is 32 random bytes (URL-safe base64). The row stores only
``sha256("pairly.pay.portal.login|<org>|<vendor>|<contact>|<token id>|<secret>")``, so
the token is bound to the organisation, the vendor, the contact and its row: changing
any part of it (or the row) makes it fail. Single use (``used_at`` is set in the same
transaction that opens the session, under a row lock), short expiry (``REQUEST_TTL``
for a link asked from the portal, ``INVITE_TTL`` for the one AP sends when it gives
access). A garbled, forged, used or expired link, a revoked contact and a closed vendor
all read the same: not valid.

Portal session (``vs1``, same shape, purpose ``session``): issued when a magic link is
used, kept by the web app in an HTTP-only cookie scoped to the vendor portal, sent to
the API in the ``X-Pairly-Vendor-Portal`` header. It never works as a staff credential
(staff routes read ``Authorization``), staff credentials never work on portal routes,
and it is not the candidate portal's session either. Absolute expiry ``SESSION_TTL``;
sign-out, revoking the contact or closing the vendor ends it.

Every portal request binds the database session to the organisation and the portal
subject ``pay_vendor:<vendor id>`` (``pairly_core.db.set_portal_subject``) before its
first query, so Row Level Security itself limits it to that vendor's rows (migration
``pay_0012``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_portal_subject, set_tenant
from pairly_core.errors import NotFoundError, PairlyError
from pairly_core.rbac import Actor, ActorKind
from pairly_pay.common import WORKFLOW, By
from pairly_pay.models import PayVendor, VendorStatus
from pairly_pay.portal.models import (
    ContactStatus,
    PayPortalContact,
    PayPortalSession,
    PayPortalToken,
)

LOGIN_PREFIX: Final = "vl1"
SESSION_PREFIX: Final = "vs1"
SUBJECT_KIND: Final = "pay_vendor"
REQUEST_TTL: Final = timedelta(minutes=30)
INVITE_TTL: Final = timedelta(days=3)
SESSION_TTL: Final = timedelta(hours=8)
SEEN_EVERY: Final = timedelta(minutes=5)
MAX_TOKEN: Final = 240
# An organisation slug (the same rule as the database lookup, core migration 0035).
ORG_SLUG_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{1,62}$"
_ORG_SLUG: Final = re.compile(ORG_SLUG_PATTERN)
ENTITY: Final = "pay.portal_contact"
# What a vendor portal actor holds (``pairly_core.rbac.vendor_may_hold``).
PORTAL_READ: Final = "pay.vendor_portal.read"
PORTAL_SUBMIT: Final = "pay.vendor_portal.submit"
PORTAL_UPDATE: Final = "pay.vendor_portal.update"
PORTAL_MESSAGE: Final = "pay.vendor_portal.message"
# A human review of a decision about the vendor's own invoice (``pairly_pay.portal.appeals``).
APPEAL_CREATE: Final = "pay.appeal.create"
PORTAL_PERMISSIONS: Final = frozenset(
    {PORTAL_READ, PORTAL_SUBMIT, PORTAL_UPDATE, PORTAL_MESSAGE, APPEAL_CREATE}
)
CLOSED_VENDOR: Final = frozenset({VendorStatus.REJECTED.value, VendorStatus.ARCHIVED.value})
INVALID: Final = "this link is not valid any more: ask for a new one"


class PortalAuthError(PairlyError):
    """No valid vendor portal session: 401, one answer for every reason."""

    status = 401
    code = "vendor_portal_session_invalid"
    title = "Sign in to the vendor portal"


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    row_id: uuid.UUID
    vendor_id: uuid.UUID
    contact_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class PortalIdentity:
    """Who a vendor portal request acts for."""

    org_id: uuid.UUID
    vendor_id: uuid.UUID
    contact_id: uuid.UUID
    session_id: uuid.UUID

    @property
    def by(self) -> By:
        return By(ActorType.VENDOR, str(self.contact_id))

    def actor(self) -> Actor:
        """The contact as an actor: only their vendor's portal, never staff rights."""
        return Actor(
            kind=ActorKind.VENDOR,
            id=self.contact_id,
            org_id=self.org_id,
            grants=(),
            permissions=PORTAL_PERMISSIONS,
        )


def portal_subject(vendor_id: uuid.UUID) -> str:
    return f"{SUBJECT_KIND}:{vendor_id}"


def _digest(
    purpose: str,
    org_id: uuid.UUID,
    vendor_id: uuid.UUID,
    contact_id: uuid.UUID,
    row_id: uuid.UUID,
    secret: str,
) -> str:
    raw = f"pairly.pay.portal.{purpose}|{org_id}|{vendor_id}|{contact_id}|{row_id}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _token(prefix: str, claim: Claim) -> str:
    return (
        f"{prefix}.{claim.org_id.hex}.{claim.row_id.hex}.{claim.vendor_id.hex}."
        f"{claim.contact_id.hex}.{claim.secret}"
    )


def parse_hex_uuid(value: str) -> uuid.UUID | None:
    """A 32 character lowercase hex id (as tokens carry them), or ``None``."""
    if len(value) != 32 or not all(c in "0123456789abcdef" for c in value):
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def org_ref(value: str) -> uuid.UUID | str | None:
    """What a portal link's ``org`` names: the organisation id (32 lowercase hex
    characters, as a link AP shared before slugs, or an expired magic link, names it), a
    slug to look up (``org_by_slug``), or ``None`` when it is neither. An id wins over a
    slug that happens to look like one."""
    found = parse_hex_uuid(value)
    if found is not None:
        return found
    return value if _ORG_SLUG.fullmatch(value) else None


def org_param(org_id: uuid.UUID, slug: str | None) -> str:
    """What a shared portal link names the organisation by: its slug, unless it has none
    or the slug would read as another id (``org_ref``); then its id in hex."""
    return slug if slug and org_ref(slug) == slug else org_id.hex


async def org_by_slug(session: AsyncSession, slug: str) -> uuid.UUID | None:
    """The active organisation with ``slug``, through ``portal_org_id_by_slug`` (core
    migration 0035, SECURITY DEFINER, returns the id only), or ``None``. Opens a
    transaction on ``session`` with no tenant and commits it, so the caller can bind the
    session to the organisation afterwards."""
    if not _ORG_SLUG.fullmatch(slug):
        return None
    found = await session.scalar(text("SELECT portal_org_id_by_slug(:slug)"), {"slug": slug})
    await session.commit()
    return found if isinstance(found, uuid.UUID) else None


async def resolve_org(session: AsyncSession, value: str) -> uuid.UUID | None:
    """The organisation a portal link's ``org`` names (``org_ref``), or ``None``. An id
    is taken as given (the sign-in sender finds nothing for an unknown one)."""
    ref = org_ref(value)
    if ref is None or isinstance(ref, uuid.UUID):
        return ref
    return await org_by_slug(session, ref)


def parse(prefix: str, token: str) -> Claim | None:
    """The parts of a ``vl1`` or ``vs1`` token, or ``None`` when it is not one."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 6 or parts[0] != prefix:
        return None
    ids = [parse_hex_uuid(p) for p in parts[1:5]]
    secret = parts[5]
    if any(i is None for i in ids):
        return None
    if len(secret) != 43 or not all(c.isalnum() or c in "-_" for c in secret):
        return None
    org_id, row_id, vendor_id, contact_id = (i for i in ids if i is not None)
    return Claim(org_id, row_id, vendor_id, contact_id, secret)


def bind(session: AsyncSession, org_id: uuid.UUID, vendor_id: uuid.UUID) -> None:
    """Bind a fresh session to the organisation and the vendor (RLS, both layers)."""
    set_tenant(session, org_id, None)
    set_portal_subject(session, portal_subject(vendor_id))


async def _usable_contact(
    session: AsyncSession, contact_id: uuid.UUID, vendor_id: uuid.UUID
) -> PayPortalContact | None:
    """The contact, when it is still active, belongs to the vendor, and the vendor is
    open (``None`` otherwise: the caller answers the same either way)."""
    contact = await session.scalar(
        select(PayPortalContact).where(PayPortalContact.id == contact_id)
    )
    if (
        contact is None
        or contact.vendor_id != vendor_id
        or contact.status != ContactStatus.ACTIVE.value
    ):
        return None
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == vendor_id))
    if vendor is None or vendor.status in CLOSED_VENDOR:
        return None
    return contact


# ------------------------------------------------------------------ magic links


async def issue_login_token(
    session: AsyncSession,
    contact: PayPortalContact,
    *,
    purpose: str,
    ttl: timedelta,
    locale: str | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """A new magic link for ``contact`` (the session is bound to its organisation)."""
    now = now or datetime.now(UTC)
    row_id = uuid.uuid4()
    secret = _secret()
    expires = now + ttl
    chosen = locale if locale in {"en", "ar"} else contact.locale
    session.add(
        PayPortalToken(
            id=row_id,
            org_id=contact.org_id,
            contact_id=contact.id,
            vendor_id=contact.vendor_id,
            secret_hash=_digest(
                "login", contact.org_id, contact.vendor_id, contact.id, row_id, secret
            ),
            purpose=purpose,
            locale=chosen,
            created_at=now,
            expires_at=expires,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="pay.vendor_portal.link_issued",
        actor_type=ActorType.SYSTEM,
        actor_id="pairly.pay.vendor_portal",
        entity_type=ENTITY,
        entity_id=contact.id,
        workflow=WORKFLOW,
        payload={
            "token_id": str(row_id),
            "vendor_id": str(contact.vendor_id),
            "purpose": purpose,
            "expires_at": expires.isoformat(),
        },
    )
    claim = Claim(contact.org_id, row_id, contact.vendor_id, contact.id, secret)
    return _token(LOGIN_PREFIX, claim), expires


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
    fresh (no transaction yet): this binds it to the token's organisation and vendor.
    Raises ``NotFoundError`` (one answer) for any link that is not valid."""
    claim = parse(LOGIN_PREFIX, token)
    if claim is None:
        raise NotFoundError(INVALID)
    bind(session, claim.org_id, claim.vendor_id)
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(PayPortalToken).where(PayPortalToken.id == claim.row_id).with_for_update()
    )
    expected = _digest(
        "login", claim.org_id, claim.vendor_id, claim.contact_id, claim.row_id, claim.secret
    )
    if (
        row is None
        or row.contact_id != claim.contact_id
        or row.vendor_id != claim.vendor_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.used_at is not None
        or row.expires_at <= now
    ):
        raise NotFoundError(INVALID)
    contact = await _usable_contact(session, claim.contact_id, claim.vendor_id)
    if contact is None:
        raise NotFoundError(INVALID)
    row.used_at = now
    contact.last_sign_in_at = now
    session_id = uuid.uuid4()
    secret = _secret()
    expires = now + SESSION_TTL
    session.add(
        PayPortalSession(
            id=session_id,
            org_id=claim.org_id,
            contact_id=claim.contact_id,
            vendor_id=claim.vendor_id,
            secret_hash=_digest(
                "session", claim.org_id, claim.vendor_id, claim.contact_id, session_id, secret
            ),
            token_id=row.id,
            created_at=now,
            expires_at=expires,
            last_seen_at=now,
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="pay.vendor_portal.signed_in",
        actor_type=ActorType.VENDOR,
        actor_id=str(claim.contact_id),
        entity_type=ENTITY,
        entity_id=claim.contact_id,
        workflow=WORKFLOW,
        payload={
            "token_id": str(row.id),
            "vendor_id": str(claim.vendor_id),
            "portal_session": str(session_id),
        },
    )
    identity = PortalIdentity(claim.org_id, claim.vendor_id, claim.contact_id, session_id)
    session_claim = Claim(claim.org_id, session_id, claim.vendor_id, claim.contact_id, secret)
    return OpenedSession(_token(SESSION_PREFIX, session_claim), identity, expires, row.locale)


# ------------------------------------------------------------------ sessions


async def authenticate(
    session: AsyncSession, token: str | None, *, now: datetime | None = None
) -> PortalIdentity:
    """The identity behind a vendor portal session token, with the database session
    bound to it. Raises ``PortalAuthError`` for anything else."""
    claim = parse(SESSION_PREFIX, token or "")
    if claim is None:
        raise PortalAuthError()
    bind(session, claim.org_id, claim.vendor_id)
    now = now or datetime.now(UTC)
    row = await session.scalar(select(PayPortalSession).where(PayPortalSession.id == claim.row_id))
    expected = _digest(
        "session", claim.org_id, claim.vendor_id, claim.contact_id, claim.row_id, claim.secret
    )
    if (
        row is None
        or row.contact_id != claim.contact_id
        or row.vendor_id != claim.vendor_id
        or not hmac.compare_digest(expected, row.secret_hash)
        or row.revoked_at is not None
        or row.expires_at <= now
    ):
        raise PortalAuthError()
    if await _usable_contact(session, claim.contact_id, claim.vendor_id) is None:
        raise PortalAuthError()
    if row.last_seen_at is None or now - row.last_seen_at > SEEN_EVERY:
        row.last_seen_at = now
        await session.flush()
    return PortalIdentity(claim.org_id, claim.vendor_id, claim.contact_id, claim.row_id)


async def sign_out(session: AsyncSession, identity: PortalIdentity) -> None:
    row = await session.scalar(
        select(PayPortalSession).where(PayPortalSession.id == identity.session_id).with_for_update()
    )
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = datetime.now(UTC)
    await session.flush()
    await append_audit(
        session,
        action="pay.vendor_portal.signed_out",
        actor_type=ActorType.VENDOR,
        actor_id=str(identity.contact_id),
        entity_type=ENTITY,
        entity_id=identity.contact_id,
        workflow=WORKFLOW,
        payload={"portal_session": str(identity.session_id)},
    )

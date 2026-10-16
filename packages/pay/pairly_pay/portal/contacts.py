# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Who at a vendor may sign in to the vendor portal, kept by the AP team.

* ``add_contact`` (``pay.vendor.write``, a person): gives portal access to one of the
  vendor's contacts on file (by email address; never an address typed in that is not on
  file, so access can never be steered to a look-alike address), and issues a magic link
  (``INVITE_TTL``) that the caller emails. Adding an address that already has access to
  this vendor sends a new link; an address with access to another vendor is refused (an
  address always signs in to one vendor).
* ``revoke_contact``: ends the access and every open portal session of that contact.

Both are audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index, blind_index_candidates, normalize_email
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.rbac import Actor
from pairly_pay.common import audit, opened
from pairly_pay.messages import vendor_contacts
from pairly_pay.models import PayVendor, VendorStatus
from pairly_pay.portal.auth import ENTITY, INVITE_TTL, issue_login_token
from pairly_pay.portal.models import ContactStatus, PayPortalContact, PayPortalSession
from pairly_pay.vendors import get_vendor


class ContactNotOnFileError(BadRequestError):
    code = "contact_not_on_file"
    title = "Only a contact on file can get portal access"


@dataclass(frozen=True, slots=True)
class Added:
    contact: PayPortalContact
    token: str
    expires_at: datetime
    created: bool


def contact_email(contact: PayPortalContact) -> str:
    return str(opened(contact, "email") or "")


def contact_name(contact: PayPortalContact) -> str:
    return str(opened(contact, "name") or "")


async def contacts_of_vendor(session: AsyncSession, vendor_id: uuid.UUID) -> list[PayPortalContact]:
    return list(
        await session.scalars(
            select(PayPortalContact)
            .where(PayPortalContact.vendor_id == vendor_id)
            .order_by(PayPortalContact.created_at.desc(), PayPortalContact.id)
            .limit(100)
        )
    )


async def add_contact(
    session: AsyncSession, actor: Actor, vendor_id: uuid.UUID, email: str, locale: str
) -> Added:
    vendor: PayVendor = await get_vendor(session, vendor_id, lock=True)
    if vendor.status != VendorStatus.ACTIVE.value:
        raise ConflictError("only an active vendor can use the portal")
    wanted = normalize_email(email)
    if wanted is None:
        raise BadRequestError("please give a valid email address")
    on_file = next((c for c in vendor_contacts(vendor) if normalize_email(c.email) == wanted), None)
    if on_file is None:
        raise ContactNotOnFileError("add the person to the vendor's contacts first")
    candidates = blind_index_candidates("email", wanted, vendor.org_id)
    existing = await session.scalar(
        select(PayPortalContact)
        .where(
            PayPortalContact.email_bidx.in_(candidates),
            PayPortalContact.status == ContactStatus.ACTIVE.value,
        )
        .with_for_update()
    )
    chosen_locale = locale if locale in {"en", "ar"} else "en"
    created = False
    if existing is not None:
        if existing.vendor_id != vendor.id:
            raise ConflictError("this address already has portal access for another vendor")
        contact = existing
        contact.locale = chosen_locale
    else:
        bidx = blind_index("email", wanted, vendor.org_id)
        if bidx is None:  # pragma: no cover - normalised above
            raise BadRequestError("please give a valid email address")
        contact_id = uuid.uuid4()
        contact = PayPortalContact(
            id=contact_id,
            org_id=vendor.org_id,
            vendor_id=vendor.id,
            email_bidx=bidx,
            locale=chosen_locale,
            status=ContactStatus.ACTIVE.value,
            created_by=actor.id,
            created_at=datetime.now(UTC),
        )
        contact.name = seal(  # type: ignore[assignment]
            PayPortalContact, "name", on_file.name, org_id=vendor.org_id, row_id=contact_id
        )
        contact.email = seal(  # type: ignore[assignment]
            PayPortalContact, "email", wanted, org_id=vendor.org_id, row_id=contact_id
        )
        session.add(contact)
        await session.flush()
        created = True
    await audit(
        session,
        actor,
        "pay.vendor_portal.contact_added" if created else "pay.vendor_portal.link_resent",
        entity_type=ENTITY,
        entity_id=contact.id,
        payload={"vendor_id": str(vendor.id), "locale": contact.locale},
    )
    token, expires = await issue_login_token(
        session, contact, purpose="invite", ttl=INVITE_TTL, locale=contact.locale
    )
    return Added(contact, token, expires, created)


async def revoke_contact(
    session: AsyncSession, actor: Actor, vendor_id: uuid.UUID, contact_id: uuid.UUID
) -> PayPortalContact:
    contact = await session.scalar(
        select(PayPortalContact)
        .where(PayPortalContact.id == contact_id, PayPortalContact.vendor_id == vendor_id)
        .with_for_update()
    )
    if contact is None:
        raise NotFoundError("portal contact not found")
    if contact.status == ContactStatus.REVOKED.value:
        return contact
    now = datetime.now(UTC)
    contact.status = ContactStatus.REVOKED.value
    contact.revoked_at = now
    contact.revoked_by = actor.id
    sessions = await session.scalars(
        select(PayPortalSession).where(
            PayPortalSession.contact_id == contact.id, PayPortalSession.revoked_at.is_(None)
        )
    )
    ended = 0
    for row in sessions:
        row.revoked_at = now
        ended += 1
    await session.flush()
    await audit(
        session,
        actor,
        "pay.vendor_portal.contact_revoked",
        entity_type=ENTITY,
        entity_id=contact.id,
        payload={"vendor_id": str(vendor_id), "sessions_ended": ended},
    )
    return contact

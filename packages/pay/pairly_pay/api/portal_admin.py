# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The AP team's side of the vendor portal (staff routes under ``/v1/pay``).

* ``GET  /vendors/{id}/portal-contacts``                who may sign in (``pay.vendor.read``)
  and the portal link to share (``portal_path``, ``/vendor-portal?org=<slug>``: the
  organisation's slug, or its id when the slug would read as one)
* ``POST /vendors/{id}/portal-contacts``                ``{"email", "locale"}``: give a
  contact on file access and email them a sign-in link (``pay.vendor.write``, a person).
  The link itself is never returned: only the contact can use it.
* ``POST /vendors/{id}/portal-contacts/{cid}/revoke``   end the access and its sessions
* ``GET  /invoices/{id}/portal-thread``                 the vendor's messages and AP's
  replies (``pay.invoice.read``)
* ``POST /invoices/{id}/portal-replies``                ``{"text"}``: reply in the portal
  (``pay.message.create``, a person). Shown at once unless the organisation's reply
  template needs an approval; then it waits for one like an exception message.
* ``POST /invoices/{id}/apply-credit``                  ``{"invoice_id"?, "note"?}``:
  apply a credit note to an invoice of the same vendor (``pay.invoice.process``, a
  person): what is paid on that invoice is reduced by the credit note's total.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.models import Organisation
from pairly_core.notifications.service import Recipient, notify
from pairly_core.rbac import authorize
from pairly_pay.api.invoices import Runtime
from pairly_pay.api.vendor_portal import web_url
from pairly_pay.api.vendors import person
from pairly_pay.common import WORKFLOW, opened
from pairly_pay.invoices import apply_credit_note, get_invoice
from pairly_pay.messages import (
    ENTITY,
    GRAPH,
    PortalReply,
    fail_message,
    post_portal_reply,
    thread_of,
)
from pairly_pay.models import MessageDirection, PayMessage
from pairly_pay.permissions import (
    INVOICE_PROCESS,
    INVOICE_READ,
    MESSAGE_CREATE,
    VENDOR_READ,
    VENDOR_WRITE,
)
from pairly_pay.portal.auth import INVITE_TTL, org_param
from pairly_pay.portal.contacts import (
    add_contact,
    contact_email,
    contact_name,
    contacts_of_vendor,
    revoke_contact,
)
from pairly_pay.portal.models import PayPortalContact
from pairly_pay.portal.templates import LOGIN_LINK
from pairly_pay.vendors import get_vendor

log = structlog.get_logger("pairly_pay.api.portal_admin")

router = APIRouter(tags=["pay"])


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayPortalAccessIn(_In):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    locale: Literal["en", "ar"] = "en"


class PayPortalAccessOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    locale: str
    status: str
    created_at: datetime
    last_sign_in_at: datetime | None
    revoked_at: datetime | None


class PayPortalAccessList(BaseModel):
    items: list[PayPortalAccessOut]
    # Where the vendor's contacts sign in (the organisation id is in the link).
    portal_path: str


class PayThreadMessageOut(BaseModel):
    id: uuid.UUID
    from_vendor: bool
    author: str
    text: str
    status: str
    at: datetime


class PayPortalReplyIn(_In):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class PayApplyCreditIn(_In):
    invoice_id: uuid.UUID | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class PayCreditAppliedOut(BaseModel):
    credit_note_id: uuid.UUID
    invoice_id: uuid.UUID
    status: str
    amount: Decimal | None
    invoice_credit_total: Decimal


def _access_out(c: PayPortalContact) -> PayPortalAccessOut:
    return PayPortalAccessOut(
        id=c.id,
        name=contact_name(c),
        email=contact_email(c),
        locale=c.locale,
        status=c.status,
        created_at=c.created_at,
        last_sign_in_at=c.last_sign_in_at,
        revoked_at=c.revoked_at,
    )


async def _thread_out(session: OrgSession, rows: list[PayMessage]) -> list[PayThreadMessageOut]:
    ids = {m.portal_contact_id for m in rows if m.portal_contact_id}
    names: dict[uuid.UUID, str] = {}
    if ids:
        for c in await session.scalars(
            select(PayPortalContact).where(PayPortalContact.id.in_(list(ids)))
        ):
            names[c.id] = contact_name(c)
    return [
        PayThreadMessageOut(
            id=m.id,
            from_vendor=m.direction == MessageDirection.INBOUND.value,
            author=names.get(m.portal_contact_id, "") if m.portal_contact_id else "",
            text=str(opened(m, "message") or ""),
            status=m.status,
            at=m.sent_at or m.created_at,
        )
        for m in rows
    ]


@router.get("/vendors/{vendor_id}/portal-contacts", response_model=PayPortalAccessList)
async def list_access(
    vendor_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayPortalAccessList:
    authorize(actor, VENDOR_READ)
    vendor = await get_vendor(session, vendor_id)
    rows = await contacts_of_vendor(session, vendor.id)
    slug = await session.scalar(select(Organisation.slug).where(Organisation.id == actor.org_id))
    return PayPortalAccessList(
        items=[_access_out(c) for c in rows],
        portal_path=f"/vendor-portal?org={org_param(actor.org_id, slug)}",
    )


@router.post(
    "/vendors/{vendor_id}/portal-contacts", status_code=201, response_model=PayPortalAccessOut
)
async def give_access(
    vendor_id: uuid.UUID,
    body: PayPortalAccessIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> JSONResponse:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    added = await add_contact(session, actor, vendor_id, body.email, body.locale)
    vendor = await get_vendor(session, vendor_id)
    org = await session.scalar(select(Organisation.name).where(Organisation.id == actor.org_id))
    contact = added.contact
    await notify(
        session,
        recipient=Recipient.external(contact_email(contact), locale=contact.locale),
        template_key=LOGIN_LINK,
        variables={
            "org_name": str(org or ""),
            "vendor_name": vendor.legal_name,
            "portal_url": f"{web_url(request)}/{contact.locale}/vendor-portal/login#{added.token}",
            "expires_minutes": int(INVITE_TTL.total_seconds() // 60),
            "expires_days": INVITE_TTL.days,
        },
        dedupe_key=f"pay.portal.invite:{uuid.uuid4()}",
        channels=["email"],
        workflow=WORKFLOW,
    )
    out = _access_out(contact)
    return JSONResponse(
        out.model_dump(mode="json"), status_code=201, headers={"Cache-Control": "no-store"}
    )


@router.post(
    "/vendors/{vendor_id}/portal-contacts/{contact_id}/revoke", response_model=PayPortalAccessOut
)
async def revoke_access(
    vendor_id: uuid.UUID, contact_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayPortalAccessOut:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    return _access_out(await revoke_contact(session, actor, vendor_id, contact_id))


@router.get("/invoices/{invoice_id}/portal-thread", response_model=list[PayThreadMessageOut])
async def portal_thread(
    invoice_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> list[PayThreadMessageOut]:
    authorize(actor, INVOICE_READ)
    invoice = await get_invoice(session, invoice_id)
    return await _thread_out(session, await thread_of(session, invoice.id))


@router.post(
    "/invoices/{invoice_id}/portal-replies",
    status_code=201,
    response_model=list[PayThreadMessageOut],
)
async def portal_reply(
    invoice_id: uuid.UUID,
    body: PayPortalReplyIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> list[PayThreadMessageOut]:
    authorize(actor, MESSAGE_CREATE)
    person(actor)
    reply: PortalReply = await post_portal_reply(session, actor, invoice_id, body.text)
    if reply.needs_approval:
        message_id = reply.message.id
        await session.commit()
        try:
            await runtime.start_run(
                actor,
                workflow=WORKFLOW,
                graph=GRAPH,
                input={"message_id": str(message_id)},
                entity_type=ENTITY,
                entity_id=str(message_id),
            )
        except Exception as exc:  # the run did not start: never sent, and it says why
            log.warning("pay_portal_reply_not_started", error=type(exc).__name__)
            await fail_message(session, message_id, f"run_not_started:{type(exc).__name__}")
            await session.commit()
    return await _thread_out(session, await thread_of(session, invoice_id))


@router.post("/invoices/{invoice_id}/apply-credit", response_model=PayCreditAppliedOut)
async def apply_credit(
    invoice_id: uuid.UUID, body: PayApplyCreditIn, actor: CurrentActor, session: OrgSession
) -> PayCreditAppliedOut:
    authorize(actor, INVOICE_PROCESS)
    person(actor)
    credit = await apply_credit_note(session, actor, invoice_id, body.invoice_id, body.note)
    target_id = credit.credit_for_invoice_id
    assert target_id is not None  # noqa: S101  (set by apply_credit_note)
    target = await get_invoice(session, target_id)
    return PayCreditAppliedOut(
        credit_note_id=credit.id,
        invoice_id=target.id,
        status=credit.status,
        amount=credit.total,
        invoice_credit_total=target.credit_total,
    )

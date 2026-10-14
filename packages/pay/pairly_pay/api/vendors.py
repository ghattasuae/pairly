# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor master routes (WP4.2). Every change is audited in the request's transaction.

* ``GET  /vendors?status=&q=&limit=``      list (``pay.vendor.read``)
* ``POST /vendors/check``                  validation and duplicate hints, no write
* ``POST /vendors``                        create (``pay.vendor.write``). The same TRN or
  IBAN as another vendor is refused (409 ``vendor_duplicate``); a similar name is
  refused unless ``acknowledge_similar``. Bank details a person enters are active at
  once: the vendor is new, nothing was paid to it yet.
* ``GET  /vendors/{id}``                   detail with accounts (masked), the risk
  profile, change tickets and, for a pending onboarding, the duplicate hints
* ``PATCH /vendors/{id}``                  edit everything but the bank details (those
  only change through a ticket). The phone number on file cannot change while a ticket
  is open; a change is recorded (``phone_changed_at``) and flags later tickets.
* ``POST /vendors/{id}/accept``            accept a pending onboarding: its bank details
  go through a change ticket (call back and dual approval); needs
  ``pay.bank_change.create`` too when there are bank details
* ``POST /vendors/{id}/reject``            reject a pending onboarding, with a reason
* ``POST /vendors/{id}/hold``              manual payment hold (``pay.hold.manage``)
* ``POST /vendors/{id}/hold/release``      release a sticky or manual hold; 409 while a
  ticket is open
* ``GET|PUT /settings``                     cooling-off hours (``pay.settings.manage``)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from pairly_core.crypto.blind_index import blind_index
from pairly_core.errors import ConflictError, ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_pay.api.schemas import (
    PayAcceptIn,
    PayContactIn,
    PayDuplicateOut,
    PayFieldCheckOut,
    PayReasonIn,
    PaySettingsIn,
    PaySettingsOut,
    PayVendorCheckIn,
    PayVendorCheckOut,
    PayVendorDetail,
    PayVendorIn,
    PayVendorList,
    PayVendorPatchIn,
)
from pairly_pay.api.views import vendor_detail, vendor_summary
from pairly_pay.bank_changes import open_ticket, open_ticket_of
from pairly_pay.common import VENDOR_ENTITY, By, audit, clean_text, record_step
from pairly_pay.holds import HoldStillNeededError, place_hold, release_hold
from pairly_pay.iban import format_iban, validate_bic, validate_iban
from pairly_pay.models import (
    AccountStatus,
    HoldReason,
    PaySettings,
    PayVendor,
    PayVendorBankAccount,
    TicketSource,
    TrnStatus,
    VendorSource,
    VendorStatus,
)
from pairly_pay.permissions import (
    BANK_CHANGE_CREATE,
    HOLD_MANAGE,
    SETTINGS_MANAGE,
    VENDOR_READ,
    VENDOR_WRITE,
)
from pairly_pay.trn import check_trn_format
from pairly_pay.vendors import (
    BankInput,
    CheckedBank,
    DuplicateVendorError,
    VendorInput,
    account_name_of,
    accounts_of,
    check_bank,
    check_phone,
    check_trn,
    create_vendor,
    find_duplicates,
    get_vendor,
    iban_of,
    seal_contacts,
    set_phone,
    settings_of,
    stored_name_key,
    vendor_filter,
)

router = APIRouter(tags=["pay"])


def person(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")
    return actor.id


def contacts_json(contacts: list[PayContactIn]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in contacts:
        phone = check_phone(c.phone) if c.phone else None
        out.append(
            {
                "name": clean_text(c.name, 120),
                "role": clean_text(c.role, 80) or None,
                "email": clean_text(c.email, 254).lower() or None,
                "phone": phone,
            }
        )
    return out


def address_json(address: Any) -> dict[str, Any]:
    data = address.model_dump(exclude_none=True) if address is not None else {}
    return {k: clean_text(str(v), 200) for k, v in data.items() if str(v).strip()}


# ------------------------------------------------------------------ list and check


@router.get("/vendors", response_model=PayVendorList)
async def list_vendors(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[Literal["pending", "active", "rejected", "archived"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    hold: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PayVendorList:
    authorize(actor, VENDOR_READ)
    query = select(PayVendor).order_by(PayVendor.legal_name, PayVendor.id)
    if status is not None:
        query = query.where(PayVendor.status == VendorStatus(status).value)
    if hold is not None:
        query = query.where(PayVendor.payment_hold.is_(hold))
    condition = vendor_filter(q)
    if condition is not None:
        query = query.where(condition)
    rows = list(await session.scalars(query.limit(limit)))
    return PayVendorList(items=[await vendor_summary(session, v) for v in rows])


@router.post("/vendors/check", response_model=PayVendorCheckOut)
async def check_vendor(
    body: PayVendorCheckIn, actor: CurrentActor, session: OrgSession
) -> PayVendorCheckOut:
    authorize(actor, VENDOR_WRITE)
    warnings: list[str] = []
    trn_out = None
    trn_value: str | None = None
    if body.trn:
        fmt = check_trn_format(body.trn)
        trn_out = PayFieldCheckOut(valid=fmt.valid, error=fmt.error, formatted=fmt.trn or None)
        trn_value = fmt.trn if fmt.valid else None
    iban_out = None
    iban_value: str | None = None
    if body.iban:
        iban = validate_iban(body.iban)
        iban_out = PayFieldCheckOut(
            valid=iban.valid,
            error=iban.error,
            country=iban.country,
            bank_code=iban.bank_code,
            formatted=format_iban(iban.iban) if iban.valid else None,
        )
        iban_value = iban.iban if iban.valid else None
    swift_out = None
    if body.swift:
        bic = validate_bic(body.swift)
        swift_out = PayFieldCheckOut(
            valid=bic.valid, error=None if bic.valid else "format", country=bic.country
        )
        if bic.valid and iban_out and iban_out.country and bic.country != iban_out.country:
            warnings.append("swift_country_differs")
    hints = await find_duplicates(
        session,
        actor.org_id,
        legal_name=body.legal_name,
        trade_name=body.trade_name,
        trn=trn_value,
        iban=iban_value,
    )
    return PayVendorCheckOut(
        duplicates=[PayDuplicateOut.model_validate(h.as_json()) for h in hints],
        trn=trn_out,
        iban=iban_out,
        swift=swift_out,
        warnings=warnings,
    )


# ------------------------------------------------------------------ create and read


@router.post("/vendors", status_code=201, response_model=PayVendorDetail)
async def create(body: PayVendorIn, actor: CurrentActor, session: OrgSession) -> PayVendorDetail:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    trn = check_trn(body.trn)
    bank = check_bank(BankInput(**body.bank.model_dump())) if body.bank else None
    phone = check_phone(body.phone_on_file)
    hints = await find_duplicates(
        session,
        actor.org_id,
        legal_name=body.legal_name,
        trade_name=body.trade_name,
        trn=trn,
        iban=bank.iban if bank else None,
    )
    if any(h.strong for h in hints):
        raise DuplicateVendorError("a vendor with the same TRN or IBAN already exists")
    if hints and not body.acknowledge_similar:
        raise DuplicateVendorError("a vendor with a similar name exists: check it first")
    vendor = await create_vendor(
        session,
        actor.org_id,
        VendorInput(
            legal_name=clean_text(body.legal_name, 200),
            country=body.country,
            legal_name_ar=clean_text(body.legal_name_ar, 200) or None,
            trade_name=clean_text(body.trade_name, 200) or None,
            trade_licence=clean_text(body.trade_licence, 64) or None,
            trn=trn,
            address=address_json(body.address),
            contacts=contacts_json(body.contacts),
            phone_on_file=phone,
            email_domains=sorted(set(body.email_domains)),
            category=clean_text(body.category, 64) or None,
        ),
        by=By.of(actor),
        bank=bank,
        source=VendorSource.STAFF,
        review_reasons=[f"similar_name:{h.reference}" for h in hints],
    )
    return await vendor_detail(session, actor, vendor)


@router.get("/vendors/{vendor_id}", response_model=PayVendorDetail)
async def get_one(
    vendor_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    authorize(actor, VENDOR_READ)
    return await vendor_detail(session, actor, await get_vendor(session, vendor_id))


@router.patch("/vendors/{vendor_id}", response_model=PayVendorDetail)
async def update(
    vendor_id: uuid.UUID, body: PayVendorPatchIn, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    vendor = await get_vendor(session, vendor_id, lock=True)
    if vendor.status in {VendorStatus.REJECTED.value, VendorStatus.ARCHIVED.value}:
        raise ConflictError("a rejected or archived vendor cannot be edited")
    now = datetime.now(UTC)
    changed: list[str] = []
    fields = body.model_fields_set
    if "legal_name" in fields and body.legal_name:
        vendor.legal_name = clean_text(body.legal_name, 200)
        changed.append("legal_name")
    for name in ("legal_name_ar", "trade_name", "trade_licence", "category"):
        if name in fields:
            limit = 64 if name in {"trade_licence", "category"} else 200
            setattr(vendor, name, clean_text(getattr(body, name), limit) or None)
            changed.append(name)
    if "legal_name" in changed or "trade_name" in changed:
        vendor.name_key = stored_name_key(vendor.legal_name, vendor.trade_name)
    if "trn" in fields:
        vendor.trn = check_trn(body.trn)
        vendor.trn_status = (TrnStatus.NOT_VERIFIED if vendor.trn else TrnStatus.NONE).value
        changed.append("trn")
    if "address" in fields:
        vendor.address = address_json(body.address)
        changed.append("address")
    if "contacts" in fields and body.contacts is not None:
        seal_contacts(vendor, contacts_json(body.contacts))
        changed.append("contacts")
    if "email_domains" in fields and body.email_domains is not None:
        vendor.email_domains = sorted(set(body.email_domains))
        changed.append("email_domains")
    phone_changed = False
    if "phone_on_file" in fields:
        phone = check_phone(body.phone_on_file)
        new_bidx = blind_index("phone", phone, vendor.org_id) if phone else None
        if new_bidx != vendor.phone_bidx and await open_ticket_of(session, vendor.id) is not None:
            # The verifier must call the number that was on file when the change opened.
            raise ConflictError(
                "the phone number on file cannot change while a bank change is open"
            )
        phone_changed = set_phone(vendor, phone, now)
        if phone_changed:
            changed.append("phone_on_file")
    vendor.updated_at = now
    await session.flush()
    if changed:
        await audit(
            session,
            actor,
            "pay.vendor.updated",
            entity_type=VENDOR_ENTITY,
            entity_id=vendor.id,
            payload={"fields": changed, "phone_on_file_changed": phone_changed},
        )
    return await vendor_detail(session, actor, vendor)


# ------------------------------------------------------------------ onboarding review


@router.post("/vendors/{vendor_id}/accept", response_model=PayVendorDetail)
async def accept(
    vendor_id: uuid.UUID, body: PayAcceptIn, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    """Accept an onboarding. Bank details it carried go through a change ticket; the
    vendor becomes active when the ticket completes (or now, without bank details)."""
    authorize(actor, VENDOR_WRITE)
    person(actor)
    vendor = await get_vendor(session, vendor_id, lock=True)
    if vendor.status != VendorStatus.PENDING.value:
        raise ConflictError("only a pending vendor can be accepted")
    if await open_ticket_of(session, vendor.id) is not None:
        raise ConflictError("this vendor's bank details are already being verified")
    accounts = await accounts_of(session, vendor.id)
    proposed = next((a for a in accounts if a.status == AccountStatus.PROPOSED.value), None)
    now = datetime.now(UTC)
    ticket_id: str | None = None
    if proposed is not None:
        authorize(actor, BANK_CHANGE_CREATE)
        bank = _bank_of(proposed)
        opened = await open_ticket(
            session,
            vendor.id,
            bank,
            source=TicketSource.ONBOARDING,
            by=By.of(actor),
            note=body.note,
            reuse_account_id=proposed.id,
        )
        ticket_id = str(opened.ticket.id)
    else:
        vendor.status = VendorStatus.ACTIVE.value
        vendor.activated_at = now
    vendor.updated_at = now
    await session.flush()
    await audit(
        session,
        actor,
        "pay.vendor.onboarding_accepted",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        payload={"ticket_id": ticket_id, "status": vendor.status},
    )
    await record_step(
        session,
        By.of(actor),
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        action="onboarding_accepted",
        inputs={"review_reasons": list(vendor.review_reasons)[:20]},
        output={"status": vendor.status, "ticket_id": ticket_id},
        reason=body.note,
    )
    return await vendor_detail(session, actor, vendor)


def _bank_of(account: PayVendorBankAccount) -> CheckedBank:
    return CheckedBank(
        iban=iban_of(account),
        account_name=account_name_of(account),
        bank_name=account.bank_name,
        swift=account.swift,
        country=account.country,
        bank_code=account.bank_code,
    )


@router.post("/vendors/{vendor_id}/reject", response_model=PayVendorDetail)
async def reject(
    vendor_id: uuid.UUID, body: PayReasonIn, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    vendor = await get_vendor(session, vendor_id, lock=True)
    if vendor.status != VendorStatus.PENDING.value:
        raise ConflictError("only a pending vendor can be rejected")
    if await open_ticket_of(session, vendor.id) is not None:
        raise ConflictError("cancel the open bank change first")
    now = datetime.now(UTC)
    for account in await accounts_of(session, vendor.id):
        if account.status == AccountStatus.PROPOSED.value:
            account.status = AccountStatus.REJECTED.value
    vendor.status = VendorStatus.REJECTED.value
    vendor.updated_at = now
    await session.flush()
    await audit(
        session,
        actor,
        "pay.vendor.onboarding_rejected",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
    )
    await record_step(
        session,
        By.of(actor),
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        action="onboarding_rejected",
        inputs={"review_reasons": list(vendor.review_reasons)[:20]},
        output={"status": vendor.status},
        reason=body.reason,
    )
    return await vendor_detail(session, actor, vendor)


# ------------------------------------------------------------------ holds


@router.post("/vendors/{vendor_id}/hold", response_model=PayVendorDetail)
async def hold(
    vendor_id: uuid.UUID, body: PayReasonIn, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    authorize(actor, HOLD_MANAGE)
    person(actor)
    vendor = await get_vendor(session, vendor_id, lock=True)
    await place_hold(session, vendor, HoldReason.MANUAL, By.of(actor), datetime.now(UTC))
    await record_step(
        session,
        By.of(actor),
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        action="payment_hold_placed",
        inputs={"previous_hold": vendor.payment_hold},
        output={"payment_hold": True, "hold_reason": vendor.hold_reason},
        reason=body.reason,
    )
    return await vendor_detail(session, actor, vendor)


@router.post("/vendors/{vendor_id}/hold/release", response_model=PayVendorDetail)
async def release(
    vendor_id: uuid.UUID, body: PayReasonIn, actor: CurrentActor, session: OrgSession
) -> PayVendorDetail:
    authorize(actor, HOLD_MANAGE)
    person(actor)
    vendor = await get_vendor(session, vendor_id, lock=True)
    previous = vendor.hold_reason
    try:
        await release_hold(session, vendor, By.of(actor), datetime.now(UTC))
    except HoldStillNeededError:
        raise ConflictError("a bank change is open: its hold stays until it closes") from None
    await record_step(
        session,
        By.of(actor),
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        action="payment_hold_released",
        inputs={"previous_reason": previous},
        output={"payment_hold": vendor.payment_hold},
        reason=body.reason,
    )
    return await vendor_detail(session, actor, vendor)


# ------------------------------------------------------------------ settings


@router.get("/settings", response_model=PaySettingsOut)
async def get_settings(actor: CurrentActor, session: OrgSession) -> PaySettingsOut:
    authorize(actor, VENDOR_READ)
    settings = await settings_of(session)
    return PaySettingsOut(
        cooling_off_hours=settings.cooling_off_hours,
        phone_change_window_days=settings.phone_change_window_days,
        can_manage=actor.has(SETTINGS_MANAGE),
    )


@router.put("/settings", response_model=PaySettingsOut)
async def put_settings(
    body: PaySettingsIn, actor: CurrentActor, session: OrgSession
) -> PaySettingsOut:
    authorize(actor, SETTINGS_MANAGE)
    person(actor)
    row = await session.scalar(select(PaySettings).with_for_update())
    previous = await settings_of(session)
    by = By.of(actor)
    now = datetime.now(UTC)
    if row is None:
        row = PaySettings(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            cooling_off_hours=body.cooling_off_hours,
            phone_change_window_days=body.phone_change_window_days,
            updated_by_type=by.actor_type.value,
            updated_by_id=by.actor_id,
            updated_at=now,
        )
        session.add(row)
    else:
        row.cooling_off_hours = body.cooling_off_hours
        row.phone_change_window_days = body.phone_change_window_days
        row.updated_by_type = by.actor_type.value
        row.updated_by_id = by.actor_id
        row.updated_at = now
    await session.flush()
    await audit(
        session,
        actor,
        "pay.settings.updated",
        entity_type="pay.settings",
        entity_id=row.id,
        payload={
            "cooling_off_hours": body.cooling_off_hours,
            "previous_cooling_off_hours": previous.cooling_off_hours,
            "phone_change_window_days": body.phone_change_window_days,
        },
    )
    return PaySettingsOut(
        cooling_off_hours=row.cooling_off_hours,
        phone_change_window_days=row.phone_change_window_days,
        can_manage=True,
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The vendor master: validation, dedupe, creation, reading and the risk profile.

* ``check_bank``: IBAN (ISO 13616, country length, structure, checksum, bank code),
  SWIFT/BIC shape, and the name on the account. A BIC whose country differs from the
  IBAN's is a warning, not an error (correspondent banks exist), shown to people.
* ``find_duplicates`` (brief 4.1.1): the same TRN, the same IBAN (blind index, nothing
  decrypted) or a similar name (``pairly_pay.dedupe``). Staff creation refuses the same
  TRN or IBAN outright, and a similar name unless the person says it is not a
  duplicate; an onboarding submission is kept with the hints for the AP review.
* ``create_vendor``: the vendor row, its first bank account and the audit event. A
  vendor a person creates gets its account ``active`` at once (no payment can have been
  made yet); details a vendor sends through the onboarding form start ``proposed``
  and only become active through a verified change ticket (``pairly_pay.bank_changes``).
  The phone number on that form is only ``phone_claimed``: the number on file is the
  one AP records from an independent source when it accepts the onboarding (M-05).
* ``risk_profile`` (brief 4.1.5): record age, recent bank changes, a recent change of
  the phone number on file, the hold, and payment history (a placeholder until the
  payment work package records payments).
* ``phone_recorder_barred``: who may not verify a change on the number on file (M-04).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index, blind_index_candidates, normalize_phone
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_pay.common import VENDOR_ENTITY, By, audit, new_reference, opened
from pairly_pay.dedupe import SIMILAR, name_key, name_similarity
from pairly_pay.iban import mask_iban, validate_bic, validate_iban
from pairly_pay.models import (
    DEFAULT_COOLING_OFF_HOURS,
    DEFAULT_PHONE_WINDOW_DAYS,
    AccountStatus,
    PayBankChangeTicket,
    PaySettings,
    PayVendor,
    PayVendorBankAccount,
    TicketStatus,
    TrnStatus,
    VendorSource,
    VendorStatus,
)
from pairly_pay.trn import check_trn_format

MAX_DUPLICATE_SCAN: Final = 5000
MAX_HINTS: Final = 10
RECENT_DAYS: Final = 90
NEW_VENDOR_DAYS: Final = 30


class InvalidBankDetailsError(BadRequestError):
    code = "bank_details_invalid"
    title = "The bank details are not valid"


class InvalidTrnError(BadRequestError):
    code = "trn_invalid"
    title = "The TRN is not valid"


class InvalidPhoneError(BadRequestError):
    code = "phone_invalid"
    title = "The phone number is not valid"


class DuplicateVendorError(ConflictError):
    code = "vendor_duplicate"
    title = "This vendor looks like one that already exists"


class VendorNotFoundError(NotFoundError):
    code = "vendor_not_found"
    title = "Vendor not found"


# ------------------------------------------------------------------ validation


@dataclass(frozen=True, slots=True)
class BankInput:
    iban: str
    account_name: str
    bank_name: str | None = None
    swift: str | None = None


@dataclass(frozen=True, slots=True)
class CheckedBank:
    iban: str
    account_name: str
    bank_name: str | None
    swift: str | None
    country: str
    bank_code: str | None
    warnings: tuple[str, ...] = ()

    @property
    def last4(self) -> str:
        return self.iban[-4:]

    @property
    def masked(self) -> str:
        return mask_iban(self.iban)


def check_bank(bank: BankInput) -> CheckedBank:
    """Valid details, or ``InvalidBankDetailsError`` naming the first problem."""
    iban = validate_iban(bank.iban)
    if not iban.valid or iban.country is None:
        raise InvalidBankDetailsError(f"iban_{iban.error}")
    account_name = " ".join((bank.account_name or "").split())
    if len(account_name) < 2:
        raise InvalidBankDetailsError("account_name_missing")
    warnings: list[str] = []
    swift: str | None = None
    if bank.swift and bank.swift.strip():
        bic = validate_bic(bank.swift)
        if not bic.valid:
            raise InvalidBankDetailsError("swift_format")
        swift = bic.bic
        if bic.country != iban.country:
            warnings.append("swift_country_differs")
    bank_name = " ".join((bank.bank_name or "").split())[:160] or None
    return CheckedBank(
        iban=iban.iban,
        account_name=account_name[:200],
        bank_name=bank_name,
        swift=swift,
        country=iban.country,
        bank_code=iban.bank_code,
        warnings=tuple(warnings),
    )


def check_trn(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    fmt = check_trn_format(value)
    if not fmt.valid:
        raise InvalidTrnError(f"trn_{fmt.error}")
    return fmt.trn


def check_phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    phone = normalize_phone(value)
    if phone is None:
        raise InvalidPhoneError("phone_format")
    return phone


# ------------------------------------------------------------------ dedupe


@dataclass(frozen=True, slots=True)
class DuplicateHint:
    vendor_id: uuid.UUID
    reference: str
    legal_name: str
    status: str
    reasons: tuple[str, ...]
    score: float

    @property
    def strong(self) -> bool:
        return "trn" in self.reasons or "iban" in self.reasons

    def as_json(self) -> dict[str, Any]:
        return {
            "vendor_id": str(self.vendor_id),
            "reference": self.reference,
            "legal_name": self.legal_name,
            "status": self.status,
            "reasons": list(self.reasons),
            "score": str(self.score),
        }


async def find_duplicates(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    legal_name: str,
    trade_name: str | None = None,
    trn: str | None = None,
    iban: str | None = None,
    exclude: uuid.UUID | None = None,
) -> list[DuplicateHint]:
    """Vendors of the organisation that may be the same company (archived ones too)."""
    found: dict[uuid.UUID, dict[str, Any]] = {}

    def hit(vendor: PayVendor, reason: str, score: float) -> None:
        if vendor.id == exclude:
            return
        entry = found.setdefault(vendor.id, {"vendor": vendor, "reasons": [], "score": 0.0})
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        entry["score"] = max(entry["score"], score)

    if trn:
        for vendor in await session.scalars(select(PayVendor).where(PayVendor.trn == trn)):
            hit(vendor, "trn", 1.0)
    if iban:
        candidates = blind_index_candidates("iban", iban, org_id)
        if candidates:
            rows = await session.scalars(
                select(PayVendor)
                .join(PayVendorBankAccount, PayVendorBankAccount.vendor_id == PayVendor.id)
                .where(
                    PayVendorBankAccount.iban_bidx.in_(candidates),
                    PayVendorBankAccount.status.in_(
                        [AccountStatus.ACTIVE.value, AccountStatus.PROPOSED.value]
                    ),
                )
            )
            for vendor in rows.unique():
                hit(vendor, "iban", 1.0)
    keys = {k for k in (name_key(legal_name), name_key(trade_name or "")) if k}
    if keys:
        names = (
            await session.execute(
                select(PayVendor.id, PayVendor.name_key)
                .order_by(PayVendor.created_at.desc())
                .limit(MAX_DUPLICATE_SCAN)
            )
        ).all()
        close: dict[uuid.UUID, float] = {}
        for row in names:
            stored = {k for k in str(row.name_key).split("|") if k}
            best = max((name_similarity(a, b) for a in keys for b in stored), default=0.0)
            if best >= SIMILAR:
                close[row.id] = best
        if close:
            for vendor in await session.scalars(
                select(PayVendor).where(PayVendor.id.in_(list(close)))
            ):
                hit(vendor, "name", close[vendor.id])
    hints = [
        DuplicateHint(
            vendor_id=v["vendor"].id,
            reference=v["vendor"].reference,
            legal_name=v["vendor"].legal_name,
            status=v["vendor"].status,
            reasons=tuple(v["reasons"]),
            score=round(float(v["score"]), 4),
        )
        for v in found.values()
    ]
    hints.sort(key=lambda h: (not h.strong, -h.score, h.reference))
    return hints[:MAX_HINTS]


def stored_name_key(legal_name: str, trade_name: str | None) -> str:
    """Both names' keys, ``|`` separated, so either can match."""
    keys = [k for k in (name_key(legal_name), name_key(trade_name or "")) if k]
    unique = list(dict.fromkeys(keys)) or [legal_name.casefold()[:100]]
    return "|".join(unique)[:300]


# ------------------------------------------------------------------ creation


@dataclass(frozen=True, slots=True)
class VendorInput:
    legal_name: str
    country: str
    legal_name_ar: str | None = None
    trade_name: str | None = None
    trade_licence: str | None = None
    trn: str | None = None
    address: dict[str, Any] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    phone_on_file: str | None = None
    email_domains: list[str] = field(default_factory=list)
    category: str | None = None
    # The number a vendor's own form gave (M-05): kept, never the number on file.
    phone_claimed: str | None = None


def seal_contacts(vendor: PayVendor, contacts: list[dict[str, Any]]) -> None:
    vendor.contacts = (
        seal(PayVendor, "contacts", contacts, org_id=vendor.org_id, row_id=vendor.id)  # type: ignore[assignment]
        if contacts
        else None
    )


def set_phone(
    vendor: PayVendor, phone: str | None, now: datetime, *, set_by: uuid.UUID | None
) -> bool:
    """Store the number on file, its blind index and who recorded it (``set_by``, the
    person; ``None`` when no staff member did). Returns whether it changed."""
    new_bidx = blind_index("phone", phone, vendor.org_id) if phone else None
    if new_bidx == vendor.phone_bidx and (phone is None) == (vendor.phone_on_file is None):
        return False
    vendor.phone_set_by = set_by if phone else None
    vendor.phone_on_file = (
        seal(PayVendor, "phone_on_file", phone, org_id=vendor.org_id, row_id=vendor.id)  # type: ignore[assignment]
        if phone
        else None
    )
    vendor.phone_bidx = new_bidx
    vendor.phone_changed_at = now
    return True


def new_account(
    vendor: PayVendor, bank: CheckedBank, status: AccountStatus, by: By, now: datetime
) -> PayVendorBankAccount:
    account_id = uuid.uuid4()
    bidx = blind_index("iban", bank.iban, vendor.org_id)
    if bidx is None:  # pragma: no cover - check_bank already validated the IBAN
        raise InvalidBankDetailsError("iban_checksum")
    return PayVendorBankAccount(
        id=account_id,
        org_id=vendor.org_id,
        vendor_id=vendor.id,
        status=status.value,
        iban=seal(PayVendorBankAccount, "iban", bank.iban, org_id=vendor.org_id, row_id=account_id),
        iban_bidx=bidx,
        iban_last4=bank.last4,
        country=bank.country,
        bank_code=bank.bank_code,
        account_name=seal(
            PayVendorBankAccount,
            "account_name",
            bank.account_name,
            org_id=vendor.org_id,
            row_id=account_id,
        ),
        bank_name=bank.bank_name,
        swift=bank.swift,
        created_by_type=by.actor_type.value,
        created_by_id=by.actor_id,
        created_at=now,
        activated_at=now if status is AccountStatus.ACTIVE else None,
    )


async def create_vendor(
    session: AsyncSession,
    org_id: uuid.UUID,
    data: VendorInput,
    *,
    by: By,
    bank: CheckedBank | None,
    source: VendorSource,
    review_reasons: list[str] | None = None,
    document_ids: list[str] | None = None,
) -> PayVendor:
    now = datetime.now(UTC)
    vendor_id = uuid.uuid4()
    status = VendorStatus.ACTIVE if source is VendorSource.STAFF else VendorStatus.PENDING
    vendor = PayVendor(
        id=vendor_id,
        org_id=org_id,
        reference=new_reference("VEN"),
        legal_name=data.legal_name,
        legal_name_ar=data.legal_name_ar,
        trade_name=data.trade_name,
        name_key=stored_name_key(data.legal_name, data.trade_name),
        country=data.country,
        trade_licence=data.trade_licence,
        trn=data.trn,
        trn_status=(TrnStatus.NOT_VERIFIED if data.trn else TrnStatus.NONE).value,
        address=data.address,
        email_domains=data.email_domains,
        category=data.category,
        status=status.value,
        source=source.value,
        payment_hold=False,
        review_reasons=review_reasons or [],
        document_ids=document_ids or [],
        created_by_type=by.actor_type.value,
        created_by_id=by.actor_id,
        created_at=now,
        updated_at=now,
        activated_at=now if status is VendorStatus.ACTIVE else None,
    )
    seal_contacts(vendor, data.contacts)
    set_phone(
        vendor,
        data.phone_on_file,
        now,
        set_by=by.user_id if source is VendorSource.STAFF else None,
    )
    vendor.phone_changed_at = None  # the first number is not a change
    if data.phone_claimed:
        vendor.phone_claimed = seal(
            PayVendor, "phone_claimed", data.phone_claimed, org_id=org_id, row_id=vendor_id
        )  # type: ignore[assignment]
    session.add(vendor)
    await session.flush()
    account: PayVendorBankAccount | None = None
    if bank is not None:
        initial = AccountStatus.ACTIVE if source is VendorSource.STAFF else AccountStatus.PROPOSED
        account = new_account(vendor, bank, initial, by, now)
        session.add(account)
        await session.flush()
    await audit(
        session,
        by,
        "pay.vendor.created",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        payload={
            "reference": vendor.reference,
            "source": source.value,
            "status": vendor.status,
            "trn_present": vendor.trn is not None,
            "bank_account_id": str(account.id) if account else None,
            "iban_last4": account.iban_last4 if account else None,
            "review_reasons": list(vendor.review_reasons)[:20],
        },
    )
    return vendor


# ------------------------------------------------------------------ reading


async def get_vendor(
    session: AsyncSession, vendor_id: uuid.UUID, *, lock: bool = False
) -> PayVendor:
    query = select(PayVendor).where(PayVendor.id == vendor_id)
    if lock:
        query = query.with_for_update()
    vendor = await session.scalar(query)
    if vendor is None:
        raise VendorNotFoundError()
    return vendor


def contacts_of(vendor: PayVendor) -> list[dict[str, Any]]:
    value = opened(vendor, "contacts")
    return list(value) if isinstance(value, list) else []


def phone_of(vendor: PayVendor) -> str | None:
    value = opened(vendor, "phone_on_file")
    return str(value) if value is not None else None


def phone_claimed_of(vendor: PayVendor) -> str | None:
    """The number the vendor's own form claimed (never called). For a vendor onboarded
    before M-05, the form's number is still the one on file, with nobody recorded."""
    value = opened(vendor, "phone_claimed")
    if value is not None:
        return str(value)
    if vendor.source == VendorSource.ONBOARDING.value and vendor.phone_set_by is None:
        return phone_of(vendor)
    return None


async def accounts_of(session: AsyncSession, vendor_id: uuid.UUID) -> list[PayVendorBankAccount]:
    return list(
        await session.scalars(
            select(PayVendorBankAccount)
            .where(PayVendorBankAccount.vendor_id == vendor_id)
            .order_by(PayVendorBankAccount.created_at.desc(), PayVendorBankAccount.id)
        )
    )


async def active_account(
    session: AsyncSession, vendor_id: uuid.UUID
) -> PayVendorBankAccount | None:
    return await session.scalar(
        select(PayVendorBankAccount).where(
            PayVendorBankAccount.vendor_id == vendor_id,
            PayVendorBankAccount.status == AccountStatus.ACTIVE.value,
        )
    )


def iban_of(account: PayVendorBankAccount) -> str:
    return str(opened(account, "iban"))


def account_name_of(account: PayVendorBankAccount) -> str:
    return str(opened(account, "account_name"))


# ------------------------------------------------------------------ settings


@dataclass(frozen=True, slots=True)
class Settings:
    cooling_off_hours: int = DEFAULT_COOLING_OFF_HOURS
    phone_change_window_days: int = DEFAULT_PHONE_WINDOW_DAYS


async def settings_of(session: AsyncSession) -> Settings:
    row = await session.scalar(select(PaySettings))
    if row is None:
        return Settings()
    return Settings(row.cooling_off_hours, row.phone_change_window_days)


# ------------------------------------------------------------------ the phone recorder


def phone_recorder_barred(
    vendor: PayVendor,
    *,
    has_verified_account: bool,
    now: datetime,
    window: timedelta,
) -> uuid.UUID | None:
    """The person who may not verify a bank-detail change on the number on file (security
    review 2026-10, M-04): whoever recorded it, while their change is within the phone-
    change window, or while the vendor has no verified (active) account yet, the number
    then being one they entered for a new vendor. ``None``: nobody is barred."""
    if vendor.phone_set_by is None or vendor.phone_on_file is None:
        return None
    if not has_verified_account:
        return vendor.phone_set_by
    if vendor.phone_changed_at is not None and now - vendor.phone_changed_at <= window:
        return vendor.phone_set_by
    return None


# ------------------------------------------------------------------ risk profile


async def risk_profile(
    session: AsyncSession, vendor: PayVendor, now: datetime | None = None
) -> dict[str, Any]:
    """What a reviewer should know about this vendor's record (brief 4.1.5)."""
    now = now or datetime.now(UTC)
    settings = await settings_of(session)
    since = now - timedelta(days=RECENT_DAYS)
    tickets = (
        await session.execute(
            select(PayBankChangeTicket.status, func.count())
            .where(
                PayBankChangeTicket.vendor_id == vendor.id,
                PayBankChangeTicket.created_at >= since,
            )
            .group_by(PayBankChangeTicket.status)
        )
    ).all()
    by_status = {str(s): int(n) for s, n in tickets}
    age_days = max(0, (now - vendor.created_at).days)
    phone_recent = vendor.phone_changed_at is not None and (
        now - vendor.phone_changed_at
    ) <= timedelta(days=settings.phone_change_window_days)
    signals: list[str] = []
    if age_days < NEW_VENDOR_DAYS:
        signals.append("new_vendor")
    if sum(by_status.values()) > 0:
        signals.append("recent_bank_change_request")
    if by_status.get(TicketStatus.REJECTED.value):
        signals.append("recent_rejected_bank_change")
    if phone_recent:
        signals.append("phone_on_file_changed_recently")
    if vendor.payment_hold:
        signals.append("payment_hold")
    if vendor.trn is None and vendor.country == "AE":
        signals.append("no_trn")
    return {
        "record_age_days": age_days,
        "bank_change_requests_90d": sum(by_status.values()),
        "bank_changes_completed_90d": by_status.get(TicketStatus.COMPLETED.value, 0),
        "bank_changes_rejected_90d": by_status.get(TicketStatus.REJECTED.value, 0),
        "phone_changed_at": vendor.phone_changed_at.isoformat()
        if vendor.phone_changed_at
        else None,
        "phone_changed_recently": phone_recent,
        # Payment history arrives with payment runs (WP4.7).
        "payment_history": {"status": "not_available"},
        "signals": signals,
    }


def masked_account(account: PayVendorBankAccount) -> str:
    return f"{account.country}** **** {account.iban_last4}"


def vendor_filter(query: str | None) -> Any:
    """Name, reference or TRN contains ``query`` (case insensitive)."""
    text = (query or "").strip()[:100]
    if not text:
        return None
    like = f"%{text.replace('%', '').replace('_', '')}%"
    return or_(
        PayVendor.legal_name.ilike(like),
        PayVendor.trade_name.ilike(like),
        PayVendor.reference.ilike(like),
        PayVendor.trn.ilike(like),
    )

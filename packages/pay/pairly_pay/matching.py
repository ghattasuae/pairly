# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Which vendor sent an invoice (brief 4.2, 4.3): TRN, IBAN, sender domain, name.

Signals, strongest first:

1. **TRN**: the vendor TRN read from the invoice equals a vendor's TRN.
2. **IBAN**: the IBAN printed on the invoice is one of a vendor's accounts (active or
   proposed), found by blind index: nothing is decrypted.
3. **Sender domain**: the email (or the forwarded original sender) came from a domain
   on a vendor's list. Exact domains only: a lookalike (``z1skar.example``) never
   matches, it simply finds nothing.
4. **Name**: the vendor name read from the invoice is close to a vendor's name
   (``pairly_pay.dedupe``).

The first signal that names exactly one vendor wins. When two strong signals name
different vendors (the TRN says one vendor, the IBAN another), nothing is linked and a
person looks: that is the shape of a fake invoice. A weak signal alone (domain or name)
links the vendor but the invoice still goes to a person (``needs_review``); the vendor
must be ``active`` for the invoice to be ``ready``. Later checks (WP4.6) compare the
bank details on the invoice with the vendor master and open a change ticket.

``prematch`` runs before extraction on the document text and the sender domains only
(TRN-shaped and IBAN-shaped strings), to pick the vendor whose earlier corrections are
given to the model as hints. It never links anything.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index_candidates
from pairly_pay.dedupe import SIMILAR, name_key, name_similarity
from pairly_pay.iban import validate_iban
from pairly_pay.models import AccountStatus, PayVendor, PayVendorBankAccount, VendorStatus
from pairly_pay.trn import check_trn_format

MAX_NAME_SCAN: Final = 5000
MAX_TEXT_CANDIDATES: Final = 20
STRONG: Final = frozenset({"trn", "iban"})
_TRN_IN_TEXT: Final = re.compile(r"(?<!\d)\d{15}(?!\d)")
_IBAN_IN_TEXT: Final = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{2,4}){3,8}\b")
_DOMAIN: Final = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass(frozen=True, slots=True)
class VendorMatch:
    vendor_id: uuid.UUID | None
    method: str | None
    status: str | None = None
    # Every vendor each signal named: {"trn": [...], "iban": [...], ...}.
    candidates: dict[str, list[str]] = field(default_factory=dict)
    conflict: bool = False

    @property
    def strong(self) -> bool:
        return self.method in STRONG

    @property
    def reasons(self) -> list[str]:
        """Why the match alone needs a person (empty when it does not)."""
        if self.conflict:
            return ["vendor_conflict:" + ",".join(sorted(self.candidates))]
        if self.vendor_id is None:
            return ["vendor_not_found"]
        out = []
        if not self.strong:
            out.append(f"vendor_weak_match:{self.method}")
        if self.status != VendorStatus.ACTIVE.value:
            out.append(f"vendor_not_active:{self.status}")
        return out

    def as_json(self) -> dict[str, Any]:
        return {
            "vendor_id": str(self.vendor_id) if self.vendor_id else None,
            "method": self.method,
            "status": self.status,
            "candidates": self.candidates,
            "conflict": self.conflict,
        }


def clean_domains(domains: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for domain in domains:
        value = (domain or "").strip().lower().rstrip(".")
        if value and _DOMAIN.fullmatch(value) and value not in out:
            out.append(value)
    return out[:5]


async def _by_trn(session: AsyncSession, trn: str | None) -> list[PayVendor]:
    if not trn:
        return []
    checked = check_trn_format(trn)
    if not checked.valid:
        return []
    return list(await session.scalars(select(PayVendor).where(PayVendor.trn == checked.trn)))


async def _by_iban(session: AsyncSession, org_id: uuid.UUID, iban: str | None) -> list[PayVendor]:
    if not iban:
        return []
    checked = validate_iban(iban)
    if not checked.valid:
        return []
    candidates = blind_index_candidates("iban", checked.iban, org_id)
    if not candidates:
        return []
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
    return list(rows.unique())


async def _by_domain(session: AsyncSession, domains: Sequence[str]) -> list[PayVendor]:
    if not domains:
        return []
    found: list[PayVendor] = []
    for domain in domains:
        rows = await session.scalars(
            select(PayVendor).where(PayVendor.email_domains.contains([domain]))
        )
        for vendor in rows:
            if vendor not in found:
                found.append(vendor)
    return found


async def _by_name(session: AsyncSession, name: str | None) -> list[PayVendor]:
    key = name_key(name or "")
    if not key:
        return []
    rows = (
        await session.execute(
            select(PayVendor.id, PayVendor.name_key)
            .order_by(PayVendor.created_at.desc())
            .limit(MAX_NAME_SCAN)
        )
    ).all()
    scored: list[tuple[float, uuid.UUID]] = []
    for row in rows:
        stored = {k for k in str(row.name_key).split("|") if k}
        best = max((name_similarity(key, other) for other in stored), default=0.0)
        if best >= SIMILAR:
            scored.append((best, row.id))
    if not scored:
        return []
    top = max(score for score, _ in scored)
    ids = [vendor_id for score, vendor_id in scored if score == top]
    return list(await session.scalars(select(PayVendor).where(PayVendor.id.in_(ids))))


def _usable(vendors: list[PayVendor]) -> list[PayVendor]:
    """Archived and rejected vendors never receive invoices."""
    return [
        v
        for v in vendors
        if v.status not in {VendorStatus.ARCHIVED.value, VendorStatus.REJECTED.value}
    ]


async def match_vendor(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    trn: str | None,
    iban: str | None,
    name: str | None,
    domains: Sequence[str],
) -> VendorMatch:
    signals = {
        "trn": _usable(await _by_trn(session, trn)),
        "iban": _usable(await _by_iban(session, org_id, iban)),
        "domain": _usable(await _by_domain(session, clean_domains(domains))),
        "name": _usable(await _by_name(session, name)),
    }
    candidates = {k: sorted(str(v.id) for v in vs) for k, vs in signals.items() if vs}
    strong_ids = {str(v.id) for key in STRONG for v in signals[key]}
    if len(strong_ids) > 1:
        return VendorMatch(None, None, candidates=candidates, conflict=True)
    for method in ("trn", "iban", "domain", "name"):
        vendors = signals[method]
        if len(vendors) == 1:
            vendor = vendors[0]
            if strong_ids and str(vendor.id) not in strong_ids:
                return VendorMatch(None, None, candidates=candidates, conflict=True)
            return VendorMatch(vendor.id, method, vendor.status, candidates)
        if len(vendors) > 1:
            # More than one vendor for this signal: only a stronger one could decide.
            continue
    return VendorMatch(None, None, candidates=candidates)


async def prematch(
    session: AsyncSession, org_id: uuid.UUID, *, text: str, domains: Sequence[str]
) -> uuid.UUID | None:
    """The one vendor the document text (TRN, IBAN) or the sender names, or None."""
    found: set[uuid.UUID] = set()
    for trn in list(dict.fromkeys(_TRN_IN_TEXT.findall(text)))[:MAX_TEXT_CANDIDATES]:
        found.update(v.id for v in _usable(await _by_trn(session, trn)))
    upper = text.upper()
    for iban in list(dict.fromkeys(_IBAN_IN_TEXT.findall(upper)))[:MAX_TEXT_CANDIDATES]:
        found.update(v.id for v in _usable(await _by_iban(session, org_id, iban)))
    if not found:
        found.update(v.id for v in _usable(await _by_domain(session, clean_domains(domains))))
    return next(iter(found)) if len(found) == 1 else None

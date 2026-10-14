# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the checks read (``InvoiceFacts``, ``History``) and what they return
(``CheckResult`` with ``Finding`` items).

Facts and history are plain frozen values built by the caller (the database adapter in
``pairly_pay.checks.service``, or the eval from synthetic ground truth), so a check never
queries anything and the same facts always give the same result.

A finding's ``code`` is the kind of problem (``near_duplicate``, ``bank_mismatch``: the
names the synthetic attack manifests use), its ``rule`` the exact rule that found it and
the key of its explanation. Evidence holds ids, numbers and short codes, never document
text.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pairly_core.guardrails.report import GuardrailReport
from pairly_core.risk.types import Severity
from pairly_pay.checks.messages import explain

EvidenceValue = str | int | bool | None
MAX_FINDINGS: Final = 40


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105 (not a password)
    FAIL = "fail"
    # The check could not run: the invoice goes to a person (fail closed).
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    code: str
    rule: str
    severity: Severity
    message_en: str
    message_ar: str
    evidence: Mapping[str, EvidenceValue] = field(default_factory=dict)
    # Duplicates: the invoice this one seems to repeat (its id).
    original_id: str | None = None
    # Other invoices in the same pattern (split invoices).
    related_ids: tuple[str, ...] = ()
    # A policy that says "a person must look" forces review whatever the severity.
    forces_review: bool = False

    @property
    def needs_review(self) -> bool:
        return self.forces_review or self.severity.rank >= Severity.HIGH.rank

    def as_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "code": self.code,
            "rule": self.rule,
            "severity": self.severity.value,
            "message": {"en": self.message_en, "ar": self.message_ar},
            "evidence": dict(self.evidence),
            "original_id": self.original_id,
            "related_ids": list(self.related_ids),
            "needs_review": self.needs_review,
        }


def finding(
    check: str,
    code: str,
    rule: str,
    severity: Severity,
    *,
    evidence: Mapping[str, EvidenceValue] | None = None,
    original_id: str | None = None,
    related_ids: tuple[str, ...] = (),
    forces_review: bool = False,
    **values: Any,
) -> Finding:
    """A finding with its explanation filled from ``values`` (plain substitution)."""
    en, ar = explain(rule, values)
    return Finding(
        check=check,
        code=code,
        rule=rule,
        severity=severity,
        message_en=en,
        message_ar=ar,
        evidence=dict(evidence or {}),
        original_id=original_id,
        related_ids=related_ids,
        forces_review=forces_review,
    )


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: str
    version: str
    status: CheckStatus
    findings: tuple[Finding, ...] = ()
    error: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.status is CheckStatus.ERROR or any(f.needs_review for f in self.findings)

    @property
    def worst(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def as_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "version": self.version,
            "status": self.status.value,
            "error": self.error,
            "findings": [f.as_json() for f in self.findings],
        }


def result(check: str, version: str, findings: list[Finding]) -> CheckResult:
    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult(check, version, status, tuple(findings[:MAX_FINDINGS]))


def error(check: str, version: str, reason: str) -> CheckResult:
    """The check could not run (fail closed). ``reason`` is a short code."""
    return CheckResult(check, version, CheckStatus.ERROR, (), reason[:120])


def run_safely(check: str, version: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """Run a pure check; any exception becomes an ``error`` result, never a pass."""
    try:
        return fn()
    except Exception as exc:
        return error(check, version, f"check_failed:{type(exc).__name__}")


# ------------------------------------------------------------------ facts


@dataclass(frozen=True, slots=True)
class Line:
    index: int
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    line_total: Decimal | None = None


@dataclass(frozen=True, slots=True)
class VendorFacts:
    id: str
    name: str
    country: str
    trn: str | None
    category: str | None
    email_domains: tuple[str, ...]
    # The active account's IBAN, normalised (no spaces, upper case); None when none.
    iban_on_file: str | None


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """What the checks read from the parsed document (``checks.document``)."""

    media_type: str
    sha256: str | None
    metadata: Mapping[str, str]
    text: str
    fonts: tuple[str, ...] = ()
    accents: tuple[str, ...] = ()
    image_count: int = 0
    page_size: str | None = None
    # Characters drawn over other characters (text pasted over text).
    overprinted: int = 0
    # Digits under a white filled box (a figure painted over).
    whiteout_digits: int = 0


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    id: str
    received_at: dt.datetime
    vendor: VendorFacts | None
    # Values as read (corrections included). None: not read.
    vendor_name: str | None = None
    vendor_trn: str | None = None
    invoice_number: str | None = None
    invoice_date: dt.date | None = None
    due_date: dt.date | None = None
    currency: str | None = None
    po_number: str | None = None
    subtotal: Decimal | None = None
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    total: Decimal | None = None
    lines: tuple[Line, ...] = ()
    bank_iban: str | None = None
    bank_account_name: str | None = None
    # The domain the invoice came from: the forwarded original sender's, else the
    # sender's. None for uploads and the API.
    origin_domain: str | None = None
    document: DocumentFacts | None = None
    guardrail_report: GuardrailReport | None = None
    # Whether anything was read at all (extraction or e-invoice).
    read: bool = True


@dataclass(frozen=True, slots=True)
class PastInvoice:
    id: str
    vendor_id: str | None
    invoice_number: str | None
    number_key: str | None
    invoice_date: dt.date | None
    currency: str | None
    total: Decimal | None
    lines_key: str | None
    sha256: str | None
    fingerprint: Mapping[str, str] | None
    received_at: dt.datetime
    status: str = "ready"


@dataclass(frozen=True, slots=True)
class PoLineFacts:
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class PoFacts:
    """The purchase order an invoice cites (``pairly_pay.checks.matching``)."""

    id: str
    number: str
    vendor_id: str
    currency: str
    status: str
    # "2-way" (against the PO) or "3-way" (against the PO and the goods received).
    match_type: str
    lines: tuple[PoLineFacts, ...]
    # Quantity received so far per PO line number, every receipt.
    received: Mapping[int, Decimal] = field(default_factory=dict)
    receipt_count: int = 0
    # Quantity per PO line that earlier invoices citing the PO took (rejected ones left out).
    invoiced_before: Mapping[int, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class History:
    """What the organisation already has, looked up by the caller before the checks."""

    # Earlier invoices of the same vendor (or of the same printed TRN or name when no
    # vendor matched), newest first, rejected ones left out.
    vendor_invoices: tuple[PastInvoice, ...] = ()
    # Earlier invoices whose document has the same file hash (any vendor).
    same_file: tuple[PastInvoice, ...] = ()
    # Other documents of the organisation with the same file hash (another workflow).
    same_file_documents: tuple[str, ...] = ()
    # Every known vendor email domain in the organisation: (vendor id, domain).
    vendor_domains: tuple[tuple[str, str], ...] = ()
    # Origin domains of earlier invoices.
    seen_domains: frozenset[str] = frozenset()
    # The purchase order the invoice cites, when one with that number is on file.
    purchase_order: PoFacts | None = None
    # The invoice's vendor has purchase orders on file (its invoices should cite one).
    vendor_uses_pos: bool = False
    # The organisation has any purchase order on file (it uses PO matching at all).
    org_uses_pos: bool = False

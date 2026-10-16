# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Duplicate and near-duplicate invoices (brief 4.4.2).

Compared with the earlier invoices of the same vendor (``History.vendor_invoices``),
each match names the suspected original and why:

* ``same_file``           the same file bytes (hash) again, or in another document;
* ``same_number``         the same invoice number once separators and case are ignored;
* ``near_number``         a number one small edit away (two adjacent digits swapped, a
                          short prefix or suffix added or removed, a zero added or
                          removed) **and** the same total or the same lines, so a
                          vendor's next number in sequence is never flagged;
* ``same_amount_window``  the same total and currency within ``duplicate_window_days``;
* ``same_lines``          the same line items (description, quantity, unit price).

``number_key`` and ``lines_key`` are stored on every invoice so later invoices can be
compared without reading earlier documents again.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal
from typing import Any, Final

from pairly_core.risk.types import Severity
from pairly_pay.checks.messages import term, terms
from pairly_pay.checks.settings import CheckSettings
from pairly_pay.checks.types import (
    CheckResult,
    Finding,
    History,
    InvoiceFacts,
    Line,
    PastInvoice,
    finding,
    result,
)

CHECK: Final = "duplicates"
VERSION: Final = "1"
MAX_AFFIX: Final = 3
_NOT_ALNUM: Final = re.compile(r"[^0-9A-Z]")
_SPACE: Final = re.compile(r"\s+")


def number_key(number: str | None) -> str | None:
    """The invoice number without separators or case (``INV-2026/001`` to
    ``INV2026001``); None when nothing is left."""
    if not number:
        return None
    folded = unicodedata.normalize("NFKC", number).upper()
    key = _NOT_ALNUM.sub("", folded)
    return key[:64] or None


def lines_key(lines: tuple[Line, ...]) -> str | None:
    """A digest of the line items (description, quantity, unit price or total)."""
    parts: list[str] = []
    for line in lines:
        if line.description is None or line.line_total is None:
            return None
        description = _SPACE.sub(" ", unicodedata.normalize("NFKC", line.description))
        quantity = f"{line.quantity.normalize():f}" if line.quantity is not None else "-"
        price = f"{line.unit_price.normalize():f}" if line.unit_price is not None else "-"
        parts.append(f"{description.strip().casefold()}|{quantity}|{price}|{line.line_total}")
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _one_transposition(a: str, b: str) -> bool:
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return (
        len(diff) == 2
        and diff[1] == diff[0] + 1
        and a[diff[0]] == b[diff[1]]
        and a[diff[1]] == b[diff[0]]
        and a[diff[0]].isdigit()
        and a[diff[1]].isdigit()
    )


def _zero_changed(a: str, b: str) -> bool:
    """``b`` is ``a`` with one ``0`` added or removed (or the other way round)."""
    if abs(len(a) - len(b)) != 1:
        return False
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return any(
        longer[i] == "0" and longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer))
    )


def _affix(a: str, b: str) -> bool:
    """One is the other with a short prefix or suffix, and the rest has a digit."""
    if a == b:
        return False
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    extra = len(longer) - len(shorter)
    if extra <= 0 or len(shorter) < 4:
        return False
    if not any(ch.isdigit() for ch in shorter):
        return False
    if longer.endswith(shorter) or longer.startswith(shorter):
        affix = longer[:extra] if longer.endswith(shorter) else longer[len(shorter) :]
        # A short affix, or a letters-only prefix (INV, R, CN) that was dropped.
        return extra <= MAX_AFFIX or affix.isalpha()
    return False


def near_number(a: str, b: str) -> str | None:
    """How number key ``a`` is a near copy of ``b``, or None."""
    if _one_transposition(a, b):
        return "transposed_digits"
    if _zero_changed(a, b):
        return "zero_changed"
    if _affix(a, b):
        return "prefix_or_suffix"
    return None


def _when(past: PastInvoice) -> str:
    return past.received_at.date().isoformat()


def _same_total(facts: InvoiceFacts, past: PastInvoice) -> bool:
    return (
        facts.total is not None
        and past.total is not None
        and facts.total == past.total
        and (facts.currency or "") == (past.currency or "")
    )


def check_duplicates(
    facts: InvoiceFacts,
    history: History,
    settings: CheckSettings,
    *,
    own_lines_key: str | None,
) -> CheckResult:
    findings: list[Finding] = []
    flagged: set[tuple[str, str]] = set()

    def add(rule: str, code: str, past: PastInvoice | None, **values: Any) -> None:
        key = (rule, past.id if past else "")
        if key in flagged:
            return
        flagged.add(key)
        evidence: dict[str, str | int | bool | None] = {"rule": rule}
        if past is not None:
            evidence |= {
                "original_id": past.id,
                "original_number": past.invoice_number,
                "original_date": past.invoice_date.isoformat() if past.invoice_date else None,
            }
        findings.append(
            finding(
                CHECK,
                code,
                rule,
                Severity.HIGH,
                evidence=evidence,
                original_id=past.id if past else None,
                **values,
            )
        )

    for past in history.same_file:
        add("same_file", "duplicate_file_hash", past, original=_when(past))
    if history.same_file_documents and not history.same_file:
        add("same_file_other", "duplicate_file_hash", None)

    key = number_key(facts.invoice_number)
    for past in history.vendor_invoices:
        if past.id == facts.id:
            continue
        same_total = _same_total(facts, past)
        same_lines = own_lines_key is not None and own_lines_key == past.lines_key
        if key is not None and past.number_key == key:
            add(
                "same_number",
                "duplicate_invoice_number",
                past,
                number=facts.invoice_number,
                original=_when(past),
            )
            continue
        how = near_number(key, past.number_key) if key and past.number_key else None
        if how is not None and (same_total or same_lines):
            same = [k for k, v in (("total", same_total), ("lines", same_lines)) if v]
            add(
                "near_number",
                "near_duplicate",
                past,
                number=facts.invoice_number,
                original_number=past.invoice_number,
                how=term(how),
                same=terms(same),
            )
            continue
        if same_lines:
            add("same_lines", "near_duplicate", past, original_number=past.invoice_number)
            continue
        if (
            same_total
            and facts.invoice_date is not None
            and past.invoice_date is not None
            and abs((facts.invoice_date - past.invoice_date).days) <= settings.duplicate_window_days
            and facts.total is not None
            and facts.total > Decimal(0)
        ):
            days = abs((facts.invoice_date - past.invoice_date).days)
            add(
                "same_amount_window",
                "near_duplicate",
                past,
                amount=f"{facts.total:,.2f}",
                currency=facts.currency,
                days=days,
                original=_when(past),
            )
    return result(CHECK, VERSION, findings)

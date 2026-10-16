# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bank details on the invoice against the vendor master (brief 4.4.3).

The printed IBAN is compared, normalised, with the vendor's active account. Any
difference is a ``bank_mismatch`` (critical): the service then blocks payment by opening
a bank-detail change ticket through ``pairly_pay.bank_changes`` (system opened, so the
vendor's payment hold is placed in the same transaction). Printed details are never used
to pay: only details that went through a verified, dual-approved ticket are.

* no IBAN printed: nothing to compare (pass);
* an IBAN that fails ISO 13616: ``bank_iban_invalid`` (high);
* no vendor matched: ``bank_unverified`` (high), nothing to compare with;
* the vendor has no active account: ``bank_not_on_file`` (critical, ticket);
* the IBAN differs from the active account: ``bank_mismatch`` (critical, ticket).

Evidence carries the last four characters only, never a full IBAN.

A credit note pays nothing, so its printed bank details are never compared and never
open a ticket: the check passes (every other check still runs on it).
"""

from __future__ import annotations

from typing import Final

from pairly_core.risk.types import Severity
from pairly_pay.checks.types import CheckResult, Finding, InvoiceFacts, finding, result
from pairly_pay.iban import normalize_iban, validate_iban

CHECK: Final = "bank"
VERSION: Final = "1"
CREDIT_NOTE: Final = "credit_note"
# Rules after which the service opens a change ticket.
TICKET_RULES: Final = frozenset({"bank_mismatch", "bank_not_on_file"})


def check_bank(facts: InvoiceFacts) -> CheckResult:
    findings: list[Finding] = []
    if not facts.bank_iban or facts.document_type == CREDIT_NOTE:
        return result(CHECK, VERSION, findings)
    printed = normalize_iban(facts.bank_iban)
    last4 = printed[-4:]
    checked = validate_iban(printed)
    if not checked.valid:
        findings.append(
            finding(
                CHECK,
                "bank_mismatch",
                "bank_iban_invalid",
                Severity.HIGH,
                evidence={"printed_last4": last4, "reason": checked.error},
                reason=checked.error,
            )
        )
        return result(CHECK, VERSION, findings)
    vendor = facts.vendor
    if vendor is None:
        findings.append(
            finding(
                CHECK,
                "bank_mismatch",
                "bank_unverified",
                Severity.HIGH,
                evidence={"printed_last4": last4},
                printed=last4,
            )
        )
    elif vendor.iban_on_file is None:
        findings.append(
            finding(
                CHECK,
                "bank_mismatch",
                "bank_not_on_file",
                Severity.CRITICAL,
                evidence={"printed_last4": last4, "vendor_id": vendor.id},
                printed=last4,
            )
        )
    elif normalize_iban(vendor.iban_on_file) != checked.iban:
        on_file = normalize_iban(vendor.iban_on_file)[-4:]
        findings.append(
            finding(
                CHECK,
                "bank_mismatch",
                "bank_mismatch",
                Severity.CRITICAL,
                evidence={
                    "printed_last4": last4,
                    "on_file_last4": on_file,
                    "vendor_id": vendor.id,
                },
                printed=last4,
                on_file=on_file,
            )
        )
    return result(CHECK, VERSION, findings)

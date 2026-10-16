# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Run the pure checks on facts and history (every check but ``policy``, which needs
the database). Used by the service and by the eval, so both run exactly the same code.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pairly_core.policy.rag import CheckOutcome, PolicyCheckResult
from pairly_core.policy.rules import evaluate_rule_set, parse_rule_set
from pairly_pay.checks import (
    amounts,
    arithmetic,
    bank,
    duplicates,
    forgery,
    impersonation,
    injection,
    matching,
    policy,
)
from pairly_pay.checks.document import document_facts
from pairly_pay.checks.settings import CheckSettings
from pairly_pay.checks.types import (
    CheckResult,
    CheckStatus,
    Finding,
    History,
    InvoiceFacts,
    error,
    run_safely,
)

__all__ = ["Keys", "bundled_policy_check", "document_facts", "keys_of", "run_pure_checks"]

PURE_CHECKS: Final = (
    "arithmetic",
    "duplicates",
    "bank",
    "impersonation",
    "amounts",
    "forgery",
    "injection",
    "matching",
)


@dataclass(frozen=True, slots=True)
class Keys:
    """What is stored on the invoice so later invoices can be compared with it."""

    number_key: str | None
    lines_key: str | None
    fingerprint: Mapping[str, str] | None


def keys_of(facts: InvoiceFacts) -> Keys:
    return Keys(
        number_key=duplicates.number_key(facts.invoice_number),
        lines_key=duplicates.lines_key(facts.lines),
        fingerprint=forgery.fingerprint(facts.document, facts.invoice_number)
        if facts.document is not None
        else None,
    )


def run_pure_checks(
    facts: InvoiceFacts,
    history: History,
    settings: CheckSettings,
    *,
    keys: Keys | None = None,
    document_required: bool = True,
) -> dict[str, CheckResult]:
    """Every pure check, each one fail closed (an exception is an ``error`` result)."""
    keys = keys or keys_of(facts)
    out: dict[str, CheckResult] = {
        "arithmetic": run_safely(
            arithmetic.CHECK,
            arithmetic.VERSION,
            lambda: arithmetic.check_arithmetic(facts, settings),
        ),
        "duplicates": run_safely(
            duplicates.CHECK,
            duplicates.VERSION,
            lambda: duplicates.check_duplicates(
                facts, history, settings, own_lines_key=keys.lines_key
            ),
        ),
        "bank": run_safely(bank.CHECK, bank.VERSION, lambda: bank.check_bank(facts)),
        "impersonation": run_safely(
            impersonation.CHECK,
            impersonation.VERSION,
            lambda: impersonation.check_impersonation(facts, history),
        ),
        "amounts": run_safely(
            amounts.CHECK, amounts.VERSION, lambda: amounts.check_amounts(facts, history, settings)
        ),
        "injection": run_safely(
            injection.CHECK, injection.VERSION, lambda: injection.check_injection(facts)
        ),
        "matching": run_safely(
            matching.CHECK,
            matching.VERSION,
            lambda: matching.check_matching(facts, history, settings),
        ),
    }
    if facts.document is None and document_required:
        out["forgery"] = error(forgery.CHECK, forgery.VERSION, "document_unavailable")
    else:
        out["forgery"] = run_safely(
            forgery.CHECK,
            forgery.VERSION,
            lambda: forgery.check_forgery(
                facts, history, settings, own_fingerprint=keys.fingerprint
            ),
        )
    return {name: out[name] for name in PURE_CHECKS}


def bundled_policy_check(facts: InvoiceFacts, as_of: dt.date) -> CheckResult:
    """The bundled rule set without a database, and no spend policy (the eval)."""
    evaluation = evaluate_rule_set(
        parse_rule_set(policy.RULES_V1), policy.rule_facts(facts), as_of=as_of
    )
    findings, problem = policy.rule_findings(evaluation)
    no_policy, _ = policy.rag_findings(
        PolicyCheckResult(CheckOutcome.NO_POLICY_FOUND, "none", "none")
    )
    all_findings: tuple[Finding, ...] = (*findings, *no_policy)
    status = CheckStatus.ERROR if problem else CheckStatus.FAIL
    return CheckResult(policy.CHECK, policy.VERSION, status, all_findings, problem)

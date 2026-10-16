# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Policy checks (brief 4.4.8): the deterministic ``pay.invoice`` rule set and the
tenant's spend policy documents.

* Rules: the latest ``pay.invoice`` rule set of the organisation (``policy_versions``,
  kind ``rules``; the bundled ``policy/invoice_rules.yaml`` is published as version 1
  the first time). Evaluated and recorded by the engine (``evaluate_and_record``: rule
  results, audit event, decision record). ``fail`` and ``needs_review`` become findings
  that force review; ``error`` makes the check an error (fail closed).
* Spend policy: ``check_against_policies`` (hybrid search over the organisation's active
  policy documents, model answer with verified citations), recorded with
  ``record_policy_check``. ``fail`` and ``needs_review`` force review; ``error`` (search
  or model down) makes the check an error. ``no_policy_found`` is information, never a
  pass: an info finding says nothing covered the invoice.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from importlib.resources import files
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.llm.types import CallContext
from pairly_core.policy.documents import active_policy_documents
from pairly_core.policy.rag import (
    CheckOutcome,
    PolicyCheckResult,
    PolicyModel,
    check_against_policies,
    record_policy_check,
)
from pairly_core.policy.rules import RULES_KIND, Outcome, RuleSetEvaluation
from pairly_core.policy.rules import Severity as RuleSeverity
from pairly_core.policy.service import Subject, evaluate_and_record, publish_rule_set
from pairly_core.policy_versions import latest_policy
from pairly_core.retrieval.embeddings import Embedder
from pairly_core.risk.types import Severity
from pairly_pay.checks.types import CheckResult, CheckStatus, Finding, InvoiceFacts, finding

CHECK: Final = "policy"
VERSION: Final = "1"
RULES_NAME: Final = "pay.invoice"
RULES_V1: Final = (
    files("pairly_pay").joinpath("policy", "invoice_rules.yaml").read_text(encoding="utf-8")
)
_SEVERITY: Final = {
    RuleSeverity.INFO: Severity.INFO,
    RuleSeverity.LOW: Severity.LOW,
    RuleSeverity.MEDIUM: Severity.MEDIUM,
    RuleSeverity.HIGH: Severity.HIGH,
    RuleSeverity.CRITICAL: Severity.CRITICAL,
}


def rule_facts(facts: InvoiceFacts) -> dict[str, Any]:
    out: dict[str, Any] = {"vendor_known": facts.vendor is not None}
    if facts.total is not None:
        out["total"] = facts.total
    if facts.currency:
        out["currency"] = facts.currency
    if facts.invoice_date is not None:
        out["invoice_date"] = facts.invoice_date
    if facts.due_date is not None:
        out["due_date"] = facts.due_date
    if facts.vendor is not None and facts.vendor.category:
        out["vendor_category"] = facts.vendor.category
    return out


def rule_findings(evaluation: RuleSetEvaluation) -> tuple[list[Finding], str | None]:
    """Findings of the rules that did not pass, and the first error code (if any)."""
    findings: list[Finding] = []
    problem: str | None = None
    for outcome in evaluation.results:
        if outcome.outcome is Outcome.PASS:
            continue
        if outcome.outcome is Outcome.ERROR:
            problem = problem or f"rule_error:{outcome.rule_id}"
            continue
        findings.append(
            finding(
                CHECK,
                "policy_rule",
                "policy_rule",
                _SEVERITY.get(outcome.severity, Severity.HIGH),
                evidence={"rule_id": outcome.rule_id, "outcome": outcome.outcome.value},
                forces_review=True,
                reason=(outcome.reason, outcome.reason_ar or outcome.reason),
            )
        )
    return findings, problem


def rag_findings(res: PolicyCheckResult) -> tuple[list[Finding], str | None]:
    """Findings of a spend policy check, and an error code when it could not run."""
    if res.outcome is CheckOutcome.ERROR:
        return [], "policy_check_error"
    if res.outcome is CheckOutcome.NO_POLICY_FOUND:
        return [finding(CHECK, "no_policy_found", "no_policy_found", Severity.INFO)], None
    findings: list[Finding] = []
    for item in res.findings:
        if item.outcome is CheckOutcome.PASS:
            continue
        rule = "policy_violated" if item.outcome is CheckOutcome.FAIL else "policy_needs_review"
        findings.append(
            finding(
                CHECK,
                "policy_violation" if rule == "policy_violated" else "policy_review",
                rule,
                Severity.HIGH,
                evidence={
                    "policy": item.citation.name[:64],
                    "version": item.citation.version,
                    "page": item.citation.page_start,
                },
                forces_review=True,
                requirement=item.requirement[:200],
            )
        )
    if res.outcome in {CheckOutcome.FAIL, CheckOutcome.NEEDS_REVIEW} and not findings:
        findings.append(
            finding(
                CHECK,
                "policy_review",
                "policy_review",
                Severity.HIGH,
                evidence={"outcome": res.outcome.value, "rejected": len(res.rejected)},
                forces_review=True,
                reason=res.outcome.value,
            )
        )
    return findings, None


async def ensure_rules(session: AsyncSession, actor_id: str) -> None:
    """Publish the bundled rule set as version 1 when the organisation has none."""
    if await latest_policy(session, RULES_KIND, RULES_NAME) is None:
        await publish_rule_set(
            session,
            name=RULES_NAME,
            content=RULES_V1,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
        )


def _question(facts: InvoiceFacts) -> str:
    kind = facts.vendor.category.replace("_", " ") if facts.vendor and facts.vendor.category else ""
    amount = f"{facts.total} {facts.currency or ''}".strip() if facts.total is not None else ""
    return (
        f"What approvals, limits or restrictions does the spend policy set for a supplier "
        f"invoice{' for ' + kind if kind else ''}{' of ' + amount if amount else ''}?"
    )


def _rag_facts(facts: InvoiceFacts) -> dict[str, Any]:
    return {
        "invoice_total": str(facts.total) if facts.total is not None else None,
        "currency": facts.currency,
        "vendor_category": facts.vendor.category if facts.vendor else None,
        "vendor_known": facts.vendor is not None,
        "has_po": bool(facts.po_number),
        "line_descriptions": [x.description for x in facts.lines[:10] if x.description],
    }


async def check_policy(
    session: AsyncSession,
    facts: InvoiceFacts,
    *,
    org_id: uuid.UUID,
    invoice_id: uuid.UUID,
    model: PolicyModel | None,
    embedder: Embedder | None,
    actor_id: str,
) -> tuple[CheckResult, list[str]]:
    """The policy check, recorded by the engine. Returns the result and the ids of the
    rule evaluations it stored."""
    subject = Subject(type="pay.invoice", id=str(invoice_id), workflow="pay")
    await ensure_rules(session, actor_id)
    recorded = await evaluate_and_record(
        session,
        name=RULES_NAME,
        facts=rule_facts(facts),
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        subject=subject,
    )
    findings, problem = rule_findings(recorded.evaluation)
    evaluations = [str(recorded.evaluation_id)]

    question = _question(facts)
    rag_facts = _rag_facts(facts)
    if not await active_policy_documents(session):
        res = PolicyCheckResult(
            CheckOutcome.NO_POLICY_FOUND, "No indexed policy document is active.", question
        )
    elif model is None or embedder is None:
        res = PolicyCheckResult(
            CheckOutcome.ERROR,
            "The policy search is not available here. A person must review this.",
            question,
        )
    else:
        res = await check_against_policies(
            session,
            facts=rag_facts,
            question=question,
            org_id=org_id,
            model=model,
            embedder=embedder,
            context=CallContext(
                actor_id=actor_id,
                workflow="pay",
                entity_type="pay.invoice",
                entity_id=str(invoice_id),
            ),
        )
    evaluations.append(
        str(
            await record_policy_check(
                session,
                res,
                facts=rag_facts,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                subject=subject,
            )
        )
    )
    more, rag_problem = rag_findings(res)
    findings += more
    problem = problem or rag_problem
    status = CheckStatus.ERROR if problem else (CheckStatus.FAIL if findings else CheckStatus.PASS)
    return CheckResult(CHECK, VERSION, status, tuple(findings), problem), evaluations


def evaluation_summary(result: CheckResult) -> Mapping[str, Any]:
    return {"status": result.status.value, "findings": len(result.findings)}

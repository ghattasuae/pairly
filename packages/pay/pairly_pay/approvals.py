# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoice approval (WP4.7, brief 4.6.1 and 4.6.2).

A person who processes invoices sends a ``ready`` invoice for approval
(``submit_for_approval``). Nothing else moves an invoice towards payment.

1. **Gates** (``pairly_pay.payability``): an active vendor, not held, no open
   bank-detail change, bank details on file, an amount, and checks that are current and
   complete. Any failure refuses the submission with the reasons.
2. **Approval by a rule** (off unless the organisation turns it on,
   ``pairly_pay.payment_settings``): only a ``low`` risk invoice that does not ask for a
   person, matched to its purchase order (the ``matching`` check passed and the PO is
   recorded on the invoice), under the configured limit, in the configured currency,
   from a low risk vendor. Recorded as a decision record of its own
   (``pay.invoice.approved_by_rule``).
   A high or critical risk invoice is never approved by a rule, whatever the amount.
3. Otherwise the ``pay_invoice_approval`` run (``pairly_pay.approval_graph``) asks the
   engine for a ``pay.invoice`` approval. The engine picks the chain from the versioned
   rules (``policy/invoice_approval_rules.yaml``) on the payload below (amount,
   currency, department, category, vendor risk, risk level), with its SLA, escalation,
   delegation and out-of-office, and its signed email links and Slack buttons.
   Segregation of duties: everyone who processed the invoice (uploaded, linked the
   vendor, coded, corrected, reviewed, sent it for approval) is a preparer, so the
   engine never lets them decide; nobody decides two steps.
4. The final action ``approve_invoice`` runs through the executor and checks again,
   from the database: the gates, that the amount, the bank account, its details and the
   check digest are the ones approved, and the segregation of duties from the decision
   rows. Only then is the invoice ``approved``, with what was approved bound to it.

A rejection closes the invoice with the approver's reason; a change request sends it
back to a person. A refused final action sends it back to a person too.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.models import ApprovalDecision
from pairly_core.approvals.rules import RULES_KIND
from pairly_core.audit import ActorType
from pairly_core.crypto.types import seal
from pairly_core.decisions.models import DecisionKind, DecisionRecord
from pairly_core.decisions.service import DecisionDraft, PolicyRef, record_decision
from pairly_core.errors import ConflictError
from pairly_core.extraction.service import load_extraction
from pairly_core.policy_versions import latest_policy
from pairly_core.rbac import Actor
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.checks.service import checks_by_name, suspected_originals
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import By, audit, record_step
from pairly_pay.invoices import (
    INVOICE_ENTITY,
    InvoiceNotFoundError,
    InvoiceStateError,
    add_reasons,
    get_invoice,
    refresh_decisions,
)
from pairly_pay.models import DocumentType, InvoiceStatus, PayInvoice, PayVendor
from pairly_pay.payability import BankOnFile, bank_on_file, blockers, same_details
from pairly_pay.payment_settings import PaymentSettings, load_payment_settings
from pairly_pay.vendors import masked_account, risk_profile

GRAPH: Final = "pay_invoice_approval"
VERSION: Final = 1
STEP_KEY: Final = "invoice_approval"
APPROVAL_TYPE: Final = "pay.invoice"
FINAL_ACTION: Final = "approve_invoice"
RULE_ACTOR: Final = "pairly.pay.approval_rule"
INVOICE_RULES_V1: Final = (
    files("pairly_pay")
    .joinpath("policy", "invoice_approval_rules.yaml")
    .read_text(encoding="utf-8")
)
# Risk levels a rule may never approve, whatever the settings or the rules say.
HUMAN_ONLY_LEVELS: Final = frozenset({"medium", "high", "critical"})
# Vendor record signals that make the vendor high or medium risk (``risk_profile``).
HIGH_VENDOR_SIGNALS: Final = frozenset(
    {"payment_hold", "recent_rejected_bank_change", "phone_on_file_changed_recently"}
)
MEDIUM_VENDOR_SIGNALS: Final = frozenset({"new_vendor", "recent_bank_change_request", "no_trn"})
# Steps of a person on the invoice that make them a preparer (decision records).
PREPARER_ACTIONS: Final = frozenset(
    {
        "pay.invoice.link_vendor",
        "pay.invoice.coded",
        "pay.invoice.corrected",
        "pay.invoice.reviewed",
        "pay.invoice.submitted_for_approval",
    }
)


class NotApprovableError(ConflictError):
    """The invoice cannot be sent for approval now; ``reasons`` says why."""

    code = "not_approvable"
    title = "Invoice cannot be approved"

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("the invoice cannot be sent for approval: " + ", ".join(reasons))
        self.reasons = list(reasons)


# ------------------------------------------------------------------ facts


def vendor_risk_level(signals: Sequence[str]) -> str:
    found = set(signals)
    if found & HIGH_VENDOR_SIGNALS:
        return "high"
    if found & MEDIUM_VENDOR_SIGNALS:
        return "medium"
    return "low"


def risk_level(invoice: PayInvoice) -> str:
    """The invoice's risk level; anything unknown counts as ``critical`` (fail closed)."""
    level = invoice.risk.get("level") if isinstance(invoice.risk, Mapping) else None
    return level if level in {"low", "medium", "high", "critical"} else "critical"


def money(value: Decimal | None, currency: str | None) -> str | None:
    """The amount at the currency's minor-unit precision (``amount_text``)."""
    return amount_text(value, currency)


async def preparers(session: AsyncSession, invoice: PayInvoice) -> list[uuid.UUID]:
    """Everyone who processed the invoice: they may never approve it."""
    people: list[uuid.UUID] = [
        p
        for p in (invoice.submitted_by, invoice.reviewed_by, invoice.approval_submitted_by)
        if p is not None
    ]
    linked = invoice.vendor_match.get("linked_by") if invoice.vendor_match else None
    if isinstance(linked, str):
        with contextlib.suppress(ValueError):
            people.append(uuid.UUID(linked))
    if invoice.extraction_id is not None:
        stored = await load_extraction(session, invoice.extraction_id)
        if stored is not None:
            people.extend(c.corrected_by for c in stored.corrections)
    rows = await session.scalars(
        select(DecisionRecord.actor_id).where(
            DecisionRecord.workflow == "pay",
            DecisionRecord.entity_type == INVOICE_ENTITY,
            DecisionRecord.entity_id == str(invoice.id),
            DecisionRecord.actor_type == ActorType.USER.value,
            DecisionRecord.final_action.in_(sorted(PREPARER_ACTIONS)),
        )
    )
    for actor_id in rows:
        try:
            people.append(uuid.UUID(actor_id))
        except ValueError:
            continue
    return list(dict.fromkeys(people))


def approval_payload(
    invoice: PayInvoice, vendor: PayVendor, bank: BankOnFile, vendor_risk: str
) -> dict[str, Any]:
    """What the approvers approve, and what the rules route on. Strings only."""
    category = invoice.category or vendor.category
    return {
        "invoice_id": str(invoice.id),
        "reference": invoice.reference,
        "vendor_id": str(vendor.id),
        "vendor_name": vendor.legal_name,
        "invoice_number": invoice.invoice_number,
        "amount": money(invoice.total, invoice.currency),
        "currency": invoice.currency,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "department": invoice.department,
        "category": category,
        "vendor_risk": vendor_risk,
        "risk_level": risk_level(invoice),
        "bank_account_id": str(bank.account.id),
        "bank_details_digest": bank.digest,
        "bank_account_masked": masked_account(bank.account),
        "checks_digest": invoice.checks_digest,
    }


def approval_context(
    invoice: PayInvoice,
    vendor: PayVendor,
    bank: BankOnFile,
    profile: Mapping[str, Any],
    vendor_risk: str,
) -> dict[str, Any]:
    """The evidence the approver sees in the inbox, the email and Slack: the fields,
    the findings, the risk and the suspected originals (the invoice page has the
    side-by-side view)."""
    findings: list[dict[str, Any]] = []
    for name, result in checks_by_name(invoice).items():
        for item in result.get("findings") or []:
            if isinstance(item, Mapping):
                findings.append(
                    {
                        "check": name,
                        "code": item.get("code"),
                        "severity": item.get("severity"),
                        "needs_review": bool(item.get("needs_review")),
                        "message": item.get("message") or {},
                    }
                )
    risk = invoice.risk if isinstance(invoice.risk, Mapping) else {}
    return {
        "kind": INVOICE_ENTITY,
        "invoice_id": str(invoice.id),
        "invoice_path": f"/pay/invoices/{invoice.id}",
        "document_id": str(invoice.document_id),
        "fields": {
            "vendor_name": invoice.vendor_name,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "po_number": invoice.po_number,
            "currency": invoice.currency,
            "subtotal": money(invoice.subtotal, invoice.currency),
            "vat_amount": money(invoice.vat_amount, invoice.currency),
            "total": money(invoice.total, invoice.currency),
            "department": invoice.department,
            "category": invoice.category or vendor.category,
        },
        "checks": {name: r.get("status") for name, r in checks_by_name(invoice).items()},
        "findings": findings[:50],
        "risk": {
            "level": risk.get("level"),
            "score": risk.get("score"),
            "requires_human": bool(risk.get("requires_human")),
            "reasons": list(risk.get("reasons") or [])[:20],
            "explanation": risk.get("explanation") or {},
        },
        "compare": [
            {"original_id": original, "rules": rules}
            for original, rules in suspected_originals(invoice)
        ][:10],
        "vendor": {
            "id": str(vendor.id),
            "reference": vendor.reference,
            "name": vendor.legal_name,
            "risk": vendor_risk,
            "signals": list(profile.get("signals") or []),
        },
        "bank": {
            "account_masked": masked_account(bank.account),
            "bank_name": bank.account.bank_name,
        },
        "decision_ids": list(invoice.decision_ids)[-20:],
    }


@dataclass(frozen=True, slots=True)
class Prepared:
    vendor: PayVendor
    bank: BankOnFile
    vendor_risk: str
    profile: dict[str, Any]
    payload: dict[str, Any]


async def prepare(session: AsyncSession, invoice: PayInvoice) -> Prepared:
    """The gates, then the payload. Raises ``NotApprovableError`` with every reason."""
    reasons = await blockers(session, invoice)
    vendor = (
        await session.scalar(select(PayVendor).where(PayVendor.id == invoice.vendor_id))
        if invoice.vendor_id
        else None
    )
    bank = await bank_on_file(session, vendor.id) if vendor is not None else None
    if reasons or vendor is None or bank is None:
        raise NotApprovableError(reasons or ["no_bank_details"])
    profile = await risk_profile(session, vendor)
    vendor_risk = vendor_risk_level(profile["signals"])
    payload = approval_payload(invoice, vendor, bank, vendor_risk)
    return Prepared(vendor, bank, vendor_risk, profile, payload)


# ------------------------------------------------------------------ approval by a rule


def rule_refusals(invoice: PayInvoice, settings: PaymentSettings, vendor_risk: str) -> list[str]:
    """Why a rule may not approve this invoice (empty: it may). Pure."""
    out: list[str] = []
    if not settings.auto_approve_low_risk:
        out.append("rule_approval_off")
    level = risk_level(invoice)
    risk = invoice.risk if isinstance(invoice.risk, Mapping) else {}
    if level in HUMAN_ONLY_LEVELS or level != "low":
        out.append(f"risk_{level}")
    if risk.get("requires_human") is not False:
        out.append("risk_requires_human")
    if invoice.total is None or not invoice.total < settings.auto_approve_limit:
        out.append("over_limit")
    if invoice.currency != settings.auto_approve_currency:
        out.append("other_currency")
    if vendor_risk != "low":
        out.append(f"vendor_risk_{vendor_risk}")
    matching = checks_by_name(invoice).get("matching") or {}
    if matching.get("status") != "pass" or invoice.po_id is None:
        out.append("not_matched")
    return out


async def _bind_approval(
    invoice: PayInvoice, prepared: Prepared, approvers: Sequence[uuid.UUID], now: datetime
) -> None:
    invoice.status = InvoiceStatus.APPROVED.value
    invoice.approved_at = now
    invoice.approver_ids = [str(a) for a in approvers]
    invoice.approved_amount = invoice.total
    invoice.approved_currency = invoice.currency
    invoice.approved_account_id = prepared.bank.account.id
    invoice.approved_bank_digest = prepared.bank.digest
    invoice.approved_checks_digest = invoice.checks_digest
    invoice.updated_at = now


async def _approve_by_rule(
    session: AsyncSession,
    invoice: PayInvoice,
    prepared: Prepared,
    settings: PaymentSettings,
) -> None:
    now = datetime.now(UTC)
    await _bind_approval(invoice, prepared, (), now)
    invoice.approved_by_rule = True
    rules = await latest_policy(session, RULES_KIND, APPROVAL_TYPE)
    refs = (
        [PolicyRef(rules.id, rules.kind, rules.name, rules.version, rules.content_hash)]
        if rules is not None
        else []
    )
    await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.POLICY_CHECK,
            workflow="pay",
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice.id),
            actor_type=ActorType.SYSTEM,
            actor_id=RULE_ACTOR,
            inputs={
                "settings": settings.as_json(),
                "risk_assessment_id": invoice.risk.get("assessment_id"),
                "payload": prepared.payload,
            },
            policy_versions=refs,
            output={"status": invoice.status, "approved_by_rule": True},
            evidence={
                "risk_level": risk_level(invoice),
                "vendor_risk": prepared.vendor_risk,
                "limit": str(settings.auto_approve_limit),
                "po_id": str(invoice.po_id) if invoice.po_id else None,
                "matching": (invoice.po_match or {}).get("status"),
            },
            final_action="pay.invoice.approved_by_rule",
        ),
    )
    await audit(
        session,
        By.system(RULE_ACTOR),
        "pay.invoice.approved_by_rule",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "amount": money(invoice.total, invoice.currency),
            "currency": invoice.currency,
            "limit": str(settings.auto_approve_limit),
            "risk_level": risk_level(invoice),
        },
    )


# ------------------------------------------------------------------ submitting


@dataclass(frozen=True, slots=True)
class Submission:
    invoice: PayInvoice
    by_rule: bool


async def submit_for_approval(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, note: str | None = None
) -> Submission:
    """A person sends a ``ready`` invoice for approval (see the module docstring). The
    caller commits, then starts the approval run unless a rule approved it."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.document_type != DocumentType.INVOICE.value:
        raise InvoiceStateError("a credit note is applied to an invoice, never paid")
    if invoice.status != InvoiceStatus.READY.value:
        raise InvoiceStateError(f"the invoice is {invoice.status}")
    prepared = await prepare(session, invoice)
    settings = await load_payment_settings(session)
    refusals = rule_refusals(invoice, settings, prepared.vendor_risk)
    now = datetime.now(UTC)
    invoice.approval_submitted_by = actor.id
    invoice.approval_submitted_at = now
    invoice.approval_run_id = None
    invoice.approval_id = None
    invoice.status = InvoiceStatus.AWAITING_APPROVAL.value
    invoice.updated_at = now
    by = By.of(actor)
    await record_step(
        session,
        by,
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.submitted_for_approval",
        inputs={"payload": prepared.payload},
        output={"status": invoice.status, "rule_refusals": refusals},
        reason=note,
    )
    await audit(
        session,
        actor,
        "pay.invoice.submitted_for_approval",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"amount": prepared.payload["amount"], "risk_level": risk_level(invoice)},
    )
    by_rule = not refusals
    if by_rule:
        await _approve_by_rule(session, invoice, prepared, settings)
    await refresh_decisions(session, invoice)
    return Submission(invoice, by_rule)


async def back_to_person(
    session: AsyncSession, invoice_id: uuid.UUID, reason: str, *, run_id: uuid.UUID | None
) -> None:
    """The approval could not go on: the invoice goes back to a person (needs review)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.status != InvoiceStatus.AWAITING_APPROVAL.value:
        return
    if run_id is not None and invoice.approval_run_id not in {None, run_id}:
        return
    invoice.status = InvoiceStatus.NEEDS_REVIEW.value
    add_reasons(invoice, [reason])
    invoice.updated_at = datetime.now(UTC)
    await audit(
        session,
        By.system(),
        "pay.invoice.approval_stopped",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"reason": reason[:200]},
    )
    await refresh_decisions(session, invoice)


async def close_after_decision(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    approval_id: uuid.UUID,
    outcome: str,
) -> None:
    """An approver rejected the invoice (closed, with their reason) or asked for changes
    (back to a person)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    if (
        invoice.status != InvoiceStatus.AWAITING_APPROVAL.value
        or invoice.approval_id != approval_id
    ):
        return
    decision = await session.scalar(
        select(ApprovalDecision)
        .where(ApprovalDecision.approval_id == approval_id)
        .order_by(ApprovalDecision.created_at.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    reason = (decision.reason if decision is not None else None) or outcome
    if outcome == "rejected":
        invoice.status = InvoiceStatus.REJECTED.value
        invoice.rejected_by = decision.actor_user_id if decision is not None else None
        invoice.rejected_at = now
        invoice.reject_reason = seal(  # type: ignore[assignment]
            PayInvoice, "reject_reason", reason, org_id=invoice.org_id, row_id=invoice.id
        )
    else:
        invoice.status = InvoiceStatus.NEEDS_REVIEW.value
        add_reasons(invoice, ["changes_requested_by_approver"])
    invoice.updated_at = now
    await audit(
        session,
        By.system(),
        f"pay.invoice.approval_{outcome}",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"approval_id": str(approval_id), "status": invoice.status},
    )
    await refresh_decisions(session, invoice)


# ------------------------------------------------------------------ the final action


async def approve_invoice(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action, inside the executor's transaction after it re-checked the
    approval, the payload hash and every decision. Checks the invoice itself again
    (fail closed): nothing about it may have changed since the approvers saw it."""
    try:
        invoice = await get_invoice(
            session, uuid.UUID(str(call.payload.get("invoice_id"))), lock=True
        )
    except (ValueError, InvoiceNotFoundError):
        raise ExecutorRefusedError("invoice_not_found") from None
    if invoice.approval_id != call.approval_id or invoice.approval_run_id != call.run_id:
        raise ExecutorRefusedError("invoice_not_of_this_approval")
    if invoice.status != InvoiceStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("invoice_not_awaiting_approval")
    try:
        prepared = await prepare(session, invoice)
    except NotApprovableError as exc:
        raise ExecutorRefusedError(f"blocked:{exc.reasons[0]}") from None
    payload = call.payload
    # Everything the approvers saw must still hold. The keyed bank digest is compared
    # by decrypting the account (any blind index key version).
    for key in sorted((set(payload) | set(prepared.payload)) - {"bank_details_digest"}):
        if payload.get(key) != prepared.payload.get(key):
            raise ExecutorRefusedError(f"changed:{key}")
    if not same_details(str(payload.get("bank_details_digest")), prepared.bank.account):
        raise ExecutorRefusedError("changed:bank_details")
    rows = (
        await session.execute(
            select(ApprovalDecision.actor_user_id, ApprovalDecision.on_behalf_of).where(
                ApprovalDecision.approval_id == call.approval_id
            )
        )
    ).all()
    involved = {r.actor_user_id for r in rows} | {r.on_behalf_of for r in rows if r.on_behalf_of}
    if set(await preparers(session, invoice)) & (involved | set(call.approver_ids)):
        raise ExecutorRefusedError("preparer_approved")
    if not call.approver_ids:
        raise ExecutorRefusedError("no_human_approver")

    now = datetime.now(UTC)
    await _bind_approval(invoice, prepared, call.approver_ids, now)
    await audit(
        session,
        By.system(),
        "pay.invoice.approved",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "approval_id": str(call.approval_id),
            "approvers": invoice.approver_ids,
            "amount": money(invoice.total, invoice.currency),
            "currency": invoice.currency,
        },
    )
    return {"invoice_id": str(invoice.id), "status": invoice.status}

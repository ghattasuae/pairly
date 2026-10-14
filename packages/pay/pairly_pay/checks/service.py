# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The invoice checks on the database: the ``CHECKS`` hook of the ``pay_invoice`` graph.

``run_invoice_checks`` runs in the ``finish`` step's transaction, after the invoice was
read and its vendor matched:

1. builds the facts (current values with corrections, vendor master, origin domain,
   parsed document, guardrail report) and the history (the vendor's earlier invoices,
   the same file, known and seen domains) of this organisation only (Row Level
   Security, and every query is org scoped by the session);
2. stores the invoice's comparison keys and runs the pure checks
   (``pairly_pay.checks.run``), each fail closed;
3. bank mismatch: opens a bank-detail change ticket through ``pairly_pay.bank_changes``
   (``source=invoice``, system opened, joining an open ticket), which places the
   vendor's payment hold in the same transaction; if no ticket can be opened (no phone
   on file, a closed vendor) the vendor is held anyway (``manual``, a person releases
   it) and a finding says why. Audited (``pay.invoice.bank_mismatch``);
4. split invoices: the related invoices that are ``ready`` go back to ``needs_review``;
5. purchase order matching (``pairly_pay.checks.matching``): the cited PO is locked for
   the transaction; the PO it resolves to and the allocation of the invoice lines to its
   lines are stored on the invoice (``po_id``, ``po_match``);
6. the policy check (rules and spend policy, recorded by the engine);
7. the risk assessment (``pairly_core.risk.service.assess`` under the ``pay.invoice``
   risk policy; it records its own decision record);
8. stores every result and the risk on the invoice, a decision record of the checks
   (``fraud_flag``) and an audit event, and returns the review reasons:
   ``check:<code>`` for findings that need a person, ``check_error:<check>`` for a
   check that could not run, ``risk:<level>`` when the risk needs a person.

Every database step that can fail runs in a savepoint, so a failure is recorded as an
error (fail closed) and the rest of the transaction stays usable.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

import structlog
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_core.documents.model import NormalizedDocument
from pairly_core.extraction.service import StoredExtraction, load_extraction
from pairly_core.guardrails.report import GuardrailReport
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.storage import ObjectStore, StorageError
from pairly_core.llm.router import ModelRouter
from pairly_core.policy_versions import latest_policy, publish_policy_version
from pairly_core.retrieval.embeddings import Embedder
from pairly_core.risk.combiner import POLICY_KIND, risk_policy_validator
from pairly_core.risk.service import assess, find_hash_matches
from pairly_core.risk.types import RiskContext, Severity
from pairly_pay.bank_changes import (
    NoPhoneOnFileError,
    SameDetailsError,
    TicketStateError,
    VendorNotOpenError,
    open_ticket,
)
from pairly_pay.checks import bank, matching, policy, risk
from pairly_pay.checks.document import document_facts
from pairly_pay.checks.duplicates import number_key
from pairly_pay.checks.run import Keys, keys_of, run_pure_checks
from pairly_pay.checks.settings import CheckSettings, parse_settings
from pairly_pay.checks.types import (
    CheckResult,
    CheckStatus,
    Finding,
    History,
    InvoiceFacts,
    Line,
    PastInvoice,
    PoFacts,
    PoLineFacts,
    VendorFacts,
    error,
    finding,
)
from pairly_pay.common import By, audit
from pairly_pay.freshness import check_inputs_digest
from pairly_pay.holds import place_hold
from pairly_pay.invoices import INVOICE_ENTITY, InvoiceNotFoundError, add_reasons, get_invoice
from pairly_pay.models import (
    HoldReason,
    InvoiceStatus,
    PayInvoice,
    PayPurchaseOrder,
    PaySettings,
    PayVendor,
    TicketSource,
    VendorStatus,
)
from pairly_pay.purchase_orders import (
    find_purchase_order,
    invoiced_quantities,
    po_lines,
    receipts_of,
    received_quantities,
)
from pairly_pay.vendors import (
    BankInput,
    InvalidBankDetailsError,
    active_account,
    check_bank,
    iban_of,
)

log = structlog.get_logger("pairly_pay.checks")

ACTOR_ID: Final = "pairly.pay.checks"
MAX_HISTORY: Final = 500
MAX_DOMAINS: Final = 5000
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
_LINE: Final = re.compile(r"^line_items\[(\d{1,3})\]\.([a-z_]+)$")
ACCOUNT_NAME_MISSING: Final = "Not printed on the invoice"


@dataclasses.dataclass(frozen=True, slots=True)
class CheckServices:
    """What the checks may use besides the session."""

    org_id: uuid.UUID
    store: ObjectStore | None = None
    router: ModelRouter | None = None
    embedder: Embedder | None = None


# ------------------------------------------------------------------ facts


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def lines_of(values: Mapping[str, Any]) -> tuple[Line, ...]:
    rows: dict[int, dict[str, Any]] = {}
    for path, value in values.items():
        match = _LINE.match(path)
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = value
    return tuple(
        Line(
            index=i,
            description=_text(row.get("description")),
            quantity=_decimal(row.get("quantity")),
            unit_price=_decimal(row.get("unit_price")),
            vat_rate=_decimal(row.get("vat_rate")),
            vat_amount=_decimal(row.get("vat_amount")),
            line_total=_decimal(row.get("line_total")),
        )
        for i, row in sorted(rows.items())
    )


def _raw(stored: StoredExtraction | None, path: str) -> str | None:
    if stored is None:
        return None
    for f in stored.fields:
        if f.path == path:
            return f.raw_value
    return None


async def _vendor_facts(session: AsyncSession, vendor_id: uuid.UUID | None) -> VendorFacts | None:
    if vendor_id is None:
        return None
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == vendor_id))
    if vendor is None:
        return None
    account = await active_account(session, vendor.id)
    return VendorFacts(
        id=str(vendor.id),
        name=vendor.legal_name,
        country=vendor.country,
        trn=vendor.trn,
        category=vendor.category,
        email_domains=tuple(d.lower() for d in vendor.email_domains),
        iban_on_file=iban_of(account) if account is not None else None,
    )


async def _normalized(
    store: ObjectStore | None, document: Document | None
) -> NormalizedDocument | None:
    if store is None or document is None:
        return None
    if document.status != DocumentStatus.PARSED or not document.parse_result_key:
        return None
    try:
        raw = await store.get_bytes(document.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        return NormalizedDocument.model_validate_json(raw)
    except (StorageError, ValidationError, ValueError):
        return None


async def build_facts(
    session: AsyncSession, invoice: PayInvoice, services: CheckServices
) -> tuple[InvoiceFacts, Document | None]:
    stored = (
        await load_extraction(session, invoice.extraction_id) if invoice.extraction_id else None
    )
    values = stored.current_values() if stored is not None else {}
    document = await session.scalar(select(Document).where(Document.id == invoice.document_id))
    normalized = await _normalized(services.store, document)
    report = (
        GuardrailReport.model_validate(invoice.guardrail_report)
        if invoice.guardrail_report
        else None
    )
    trn = _text(values.get("vendor_trn")) or _text(_raw(stored, "vendor_trn"))
    date = invoice.invoice_date
    due = invoice.due_date
    if date is None and isinstance(values.get("invoice_date"), str):
        try:
            date = datetime.fromisoformat(values["invoice_date"]).date()
        except ValueError:
            date = None
    facts = InvoiceFacts(
        id=str(invoice.id),
        received_at=invoice.created_at,
        vendor=await _vendor_facts(session, invoice.vendor_id),
        vendor_name=_text(values.get("vendor_name")),
        vendor_trn=trn,
        invoice_number=_text(values.get("invoice_number")),
        invoice_date=date,
        due_date=due,
        currency=_text(values.get("currency")),
        po_number=_text(values.get("po_number")),
        subtotal=_decimal(values.get("subtotal")),
        vat_rate=_decimal(values.get("vat_rate")),
        vat_amount=_decimal(values.get("vat_amount")),
        total=_decimal(values.get("total")),
        lines=lines_of(values),
        bank_iban=_text(values.get("bank.iban")) or _text(_raw(stored, "bank.iban")),
        bank_account_name=_text(values.get("bank.account_name")),
        origin_domain=invoice.sender_domains[0] if invoice.sender_domains else None,
        document=document_facts(normalized, document.sha256 if document else None)
        if normalized is not None
        else None,
        guardrail_report=report,
        read=stored is not None,
    )
    return facts, document


def _past(row: PayInvoice, sha256: str | None = None) -> PastInvoice:
    return PastInvoice(
        id=str(row.id),
        vendor_id=str(row.vendor_id) if row.vendor_id else None,
        invoice_number=row.invoice_number,
        number_key=row.number_key or number_key(row.invoice_number),
        invoice_date=row.invoice_date,
        currency=row.currency,
        total=row.total,
        lines_key=row.lines_key,
        sha256=sha256,
        fingerprint=row.fingerprint,
        received_at=row.created_at,
        status=row.status,
    )


async def build_history(
    session: AsyncSession, invoice: PayInvoice, facts: InvoiceFacts, document: Document | None
) -> History:
    earlier = (
        select(PayInvoice)
        .where(
            PayInvoice.id != invoice.id,
            PayInvoice.status != InvoiceStatus.REJECTED.value,
            PayInvoice.created_at <= invoice.created_at,
        )
        .order_by(PayInvoice.created_at.desc(), PayInvoice.id)
        .limit(MAX_HISTORY)
    )
    scope: list[Any] = []
    if invoice.vendor_id is not None:
        scope.append(PayInvoice.vendor_id == invoice.vendor_id)
    else:
        if invoice.vendor_trn:
            scope.append(PayInvoice.vendor_trn == invoice.vendor_trn)
        if facts.vendor_name:
            scope.append(PayInvoice.vendor_name == facts.vendor_name[:200])
    vendor_rows = list(await session.scalars(earlier.where(or_(*scope)))) if scope else []

    same_file: list[PastInvoice] = []
    other_docs: list[str] = []
    if document is not None:
        matches = await find_hash_matches(session, document.sha256, exclude_document_id=document.id)
        ids = [uuid.UUID(m.entity_id) for m in matches]
        if ids:
            rows = await session.scalars(
                select(PayInvoice).where(
                    PayInvoice.document_id.in_(ids), PayInvoice.id != invoice.id
                )
            )
            same_file = [_past(r, document.sha256) for r in rows]
            linked = {p.id for p in same_file}
            other_docs = [m.entity_id for m in matches if m.entity_id not in linked]

    vendor_domains: list[tuple[str, str]] = []
    vendors = await session.execute(
        select(PayVendor.id, PayVendor.email_domains)
        .where(PayVendor.status.notin_([VendorStatus.REJECTED.value, VendorStatus.ARCHIVED.value]))
        .limit(MAX_DOMAINS)
    )
    for vendor_id, domains in vendors.all():
        vendor_domains += [(str(vendor_id), str(d).lower()) for d in domains or []]
    seen_rows = await session.scalars(
        select(PayInvoice.sender_domains)
        .where(PayInvoice.id != invoice.id, PayInvoice.created_at <= invoice.created_at)
        .order_by(PayInvoice.created_at.desc())
        .limit(MAX_DOMAINS)
    )
    seen = frozenset(str(d[0]).lower() for d in seen_rows if d)
    uses_pos = (
        invoice.vendor_id is not None
        and (
            await session.scalar(
                select(PayPurchaseOrder.id)
                .where(PayPurchaseOrder.vendor_id == invoice.vendor_id)
                .limit(1)
            )
        )
        is not None
    )
    return History(
        vendor_invoices=tuple(_past(r) for r in vendor_rows),
        same_file=tuple(same_file),
        same_file_documents=tuple(other_docs),
        vendor_domains=tuple(vendor_domains),
        seen_domains=seen,
        purchase_order=await purchase_order_facts(session, invoice, facts.po_number),
        vendor_uses_pos=uses_pos,
        org_uses_pos=uses_pos
        or (await session.scalar(select(PayPurchaseOrder.id).limit(1))) is not None,
    )


async def purchase_order_facts(
    session: AsyncSession, invoice: PayInvoice, po_number: str | None
) -> PoFacts | None:
    """The PO the invoice cites, locked for this transaction (two invoices of one PO are
    matched one after the other, never both against the same remaining quantity), with
    what was received and what earlier invoices citing it took."""
    po = await find_purchase_order(session, po_number, lock=True)
    if po is None:
        return None
    return PoFacts(
        id=str(po.id),
        number=po.po_number,
        vendor_id=str(po.vendor_id),
        currency=po.currency,
        status=po.status,
        match_type=po.match_type,
        lines=tuple(
            PoLineFacts(line.line_no, line.description, line.quantity, line.unit_price)
            for line in await po_lines(session, po.id)
        ),
        received=await received_quantities(session, po.id),
        receipt_count=len(await receipts_of(session, po.id)),
        invoiced_before=await invoiced_quantities(session, po.id, before=invoice),
    )


async def load_settings(session: AsyncSession) -> CheckSettings:
    row = await session.scalar(select(PaySettings))
    return parse_settings(row.check_settings if row is not None else None)


# ------------------------------------------------------------------ actions


async def _open_bank_ticket(
    session: AsyncSession, invoice: PayInvoice, facts: InvoiceFacts, outcome: CheckResult
) -> CheckResult:
    """Block payment for a bank mismatch: a change ticket (hold included), audited."""
    needs = [f for f in outcome.findings if f.rule in bank.TICKET_RULES]
    if not needs or invoice.vendor_id is None or not facts.bank_iban:
        return outcome
    by = By.system(ACTOR_ID)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {"vendor_id": str(invoice.vendor_id)}
    extra: list[Finding] = []
    try:
        async with session.begin_nested():
            checked = check_bank(
                BankInput(
                    iban=facts.bank_iban,
                    account_name=facts.bank_account_name or ACCOUNT_NAME_MISSING,
                )
            )
            opened = await open_ticket(
                session,
                invoice.vendor_id,
                checked,
                source=TicketSource.INVOICE,
                by=by,
                note=f"Bank details on invoice {invoice.reference} differ from the vendor master.",
                claims={"invoice": invoice.reference},
                join_open=True,
            )
            payload |= {
                "ticket_id": str(opened.ticket.id),
                "ticket_reference": opened.ticket.reference,
                "created": opened.created,
            }
    except (
        NoPhoneOnFileError,
        VendorNotOpenError,
        InvalidBankDetailsError,
        SameDetailsError,
        TicketStateError,
    ) as exc:
        reason = type(exc).__name__
        payload |= {"ticket_error": reason}
        async with session.begin_nested():
            vendor = await session.scalar(
                select(PayVendor).where(PayVendor.id == invoice.vendor_id).with_for_update()
            )
            if vendor is not None:
                await place_hold(session, vendor, HoldReason.MANUAL, by, now)
                payload |= {"held": True}
        extra.append(
            finding(
                bank.CHECK,
                "bank_mismatch",
                "bank_ticket_failed",
                Severity.CRITICAL,
                evidence={"reason": reason},
                reason=reason,
            )
        )
    await audit(
        session, by, "pay.invoice.bank_mismatch", entity_type=INVOICE_ENTITY, entity_id=invoice.id,
        payload=payload,
    )  # fmt: skip
    ticket = payload.get("ticket_id")
    findings = tuple(
        dataclasses.replace(f, evidence={**f.evidence, "ticket_id": ticket})
        if f.rule in bank.TICKET_RULES and ticket
        else f
        for f in outcome.findings
    )
    return dataclasses.replace(outcome, findings=(*findings, *extra))


async def _flag_related(session: AsyncSession, invoice: PayInvoice, outcome: CheckResult) -> None:
    """Split invoices: the related invoices that were ready go back to a person."""
    related = {r for f in outcome.findings for r in f.related_ids}
    for related_id in sorted(related)[:20]:
        try:
            other = await get_invoice(session, uuid.UUID(related_id), lock=True)
        except (InvoiceNotFoundError, ValueError):
            continue
        before = other.status
        if before in {InvoiceStatus.READY.value, InvoiceStatus.NEEDS_REVIEW.value}:
            add_reasons(other, ["check:split_under_threshold"])
            other.status = InvoiceStatus.NEEDS_REVIEW.value
            other.updated_at = datetime.now(UTC)
        await audit(
            session,
            By.system(ACTOR_ID),
            "pay.invoice.flagged_by_related",
            entity_type=INVOICE_ENTITY,
            entity_id=other.id,
            payload={"related_invoice_id": str(invoice.id), "previous_status": before},
        )


async def _ensure_risk_policy(session: AsyncSession) -> None:
    if await latest_policy(session, POLICY_KIND, risk.ASSESSMENT_TYPE) is None:
        await publish_policy_version(
            session,
            kind=POLICY_KIND,
            name=risk.ASSESSMENT_TYPE,
            content=risk.RISK_POLICY_V1,
            validate=risk_policy_validator(risk.ASSESSMENT_TYPE),
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
        )


def review_reasons(results: Mapping[str, CheckResult], risk_json: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for name, outcome in results.items():
        if outcome.status is CheckStatus.ERROR:
            reasons.append(f"check_error:{name}:{outcome.error or 'error'}")
        for f in outcome.findings:
            if f.needs_review:
                reasons.append(f"check:{f.code}")
    if risk_json.get("requires_human"):
        reasons.append(f"risk:{risk_json.get('level', 'high')}")
    return list(dict.fromkeys(r[:200] for r in reasons))


# ------------------------------------------------------------------ the hook


async def run_invoice_checks(
    session: AsyncSession, invoice: PayInvoice, services: CheckServices
) -> list[str]:
    """Run every check on ``invoice`` (locked by the caller). Returns review reasons."""
    settings = await load_settings(session)
    # What these results are about (``pairly_pay.freshness``): taken before anything runs.
    inputs_digest = await check_inputs_digest(session, invoice)
    facts, document = await build_facts(session, invoice, services)
    history = await build_history(session, invoice, facts, document)
    keys: Keys = keys_of(facts)
    invoice.number_key = keys.number_key
    invoice.lines_key = keys.lines_key
    invoice.fingerprint = dict(keys.fingerprint) if keys.fingerprint else None
    results = run_pure_checks(facts, history, settings, keys=keys)
    try:
        matched = matching.match_invoice(facts, history, settings)
        results["matching"] = matched.result
        invoice.po_id = uuid.UUID(matched.po_id) if matched.po_id else None
        invoice.po_match = matched.as_json()
    except Exception as exc:  # fail closed: never matched
        log.error("pay.checks.matching_failed", error=type(exc).__name__)
        results["matching"] = error(
            matching.CHECK, matching.VERSION, f"check_failed:{type(exc).__name__}"
        )
        invoice.po_id = None
        invoice.po_match = {"status": "error"}

    try:
        results["bank"] = await _open_bank_ticket(session, invoice, facts, results["bank"])
    except Exception as exc:  # the block must not fail silently: a person looks
        log.error("pay.checks.bank_action_failed", error=type(exc).__name__)
        results["bank"] = error(bank.CHECK, bank.VERSION, f"ticket_failed:{type(exc).__name__}")
    if any(f.related_ids for f in results["amounts"].findings):
        try:
            async with session.begin_nested():
                await _flag_related(session, invoice, results["amounts"])
        except Exception as exc:
            log.error("pay.checks.related_failed", error=type(exc).__name__)

    evaluations: list[str] = []
    try:
        async with session.begin_nested():
            results["policy"], evaluations = await policy.check_policy(
                session,
                facts,
                org_id=services.org_id,
                invoice_id=invoice.id,
                model=services.router,
                embedder=services.embedder,
                actor_id=ACTOR_ID,
            )
    except Exception as exc:
        log.error("pay.checks.policy_failed", error=type(exc).__name__)
        results["policy"] = error(
            policy.CHECK, policy.VERSION, f"check_failed:{type(exc).__name__}"
        )

    risk_json: dict[str, Any]
    try:
        async with session.begin_nested():
            await _ensure_risk_policy(session)
            record = await assess(
                session,
                RiskContext(
                    org_id=services.org_id,
                    workflow="pay",
                    entity_type=INVOICE_ENTITY,
                    entity_id=str(invoice.id),
                    document_sha256=document.sha256 if document is not None else None,
                    amount=facts.total,
                    facts={"checks": dict(results)},
                ),
                risk.PAY_DETECTORS.select(),
                assessment_type=risk.ASSESSMENT_TYPE,
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR_ID,
            )
            risk_json = risk.as_json(record.combined, str(record.id))
    except Exception as exc:
        log.error("pay.checks.risk_failed", error=type(exc).__name__)
        risk_json = {
            "assessment_id": None,
            "level": "high",
            "requires_human": True,
            "reasons": [f"risk_failed:{type(exc).__name__}"],
        }

    now = datetime.now(UTC)
    invoice.checks = [results[name].as_json() for name in risk.CHECK_NAMES if name in results]
    invoice.risk = risk_json
    invoice.checked_at = now
    invoice.checks_digest = inputs_digest
    reasons = review_reasons(results, risk_json)
    statuses = {name: r.status.value for name, r in results.items()}
    await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.FRAUD_FLAG,
            workflow="pay",
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice.id),
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            inputs={
                "extraction_id": str(invoice.extraction_id) if invoice.extraction_id else None,
                "document_sha256": document.sha256 if document is not None else None,
                "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
                "settings": settings.as_json(),
                "history": {
                    "vendor_invoices": len(history.vendor_invoices),
                    "same_file": len(history.same_file),
                    "known_domains": len(history.vendor_domains),
                },
                "rule_evaluations": evaluations,
                "purchase_order": {
                    "po_id": str(invoice.po_id) if invoice.po_id else None,
                    "po_number": history.purchase_order.number if history.purchase_order else None,
                    "match": invoice.po_match.get("status"),
                    "lines": invoice.po_match.get("lines"),
                },
                "checks_digest": inputs_digest,
                "risk_assessment_id": risk_json.get("assessment_id"),
            },
            output={
                "checks": statuses,
                "review_reasons": reasons,
                "risk_level": risk_json.get("level"),
                "requires_human": bool(reasons),
            },
            evidence={
                "findings": [f.as_json() for r in results.values() for f in r.findings][:200]
            },
        ),
    )
    await audit(
        session,
        By.system(ACTOR_ID),
        "pay.invoice.checked",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "checks": statuses,
            "findings": sorted({f.code for r in results.values() for f in r.findings})[:40],
            "risk_level": risk_json.get("level"),
            "review_reasons": reasons[:20],
        },
    )
    return reasons


def checks_by_name(invoice: PayInvoice) -> dict[str, dict[str, Any]]:
    return {
        str(c.get("check")): c for c in invoice.checks if isinstance(c, dict) and c.get("check")
    }


def suspected_originals(invoice: PayInvoice) -> list[tuple[str, list[str]]]:
    """The suspected originals of a duplicate, with the rules that point at each."""
    out: dict[str, list[str]] = {}
    duplicates = checks_by_name(invoice).get("duplicates") or {}
    for item in duplicates.get("findings") or []:
        if isinstance(item, dict) and item.get("original_id"):
            out.setdefault(str(item["original_id"]), []).append(str(item.get("rule", "")))
    return list(out.items())


def ids(values: Sequence[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in values:
        try:
            out.append(uuid.UUID(value))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------------ a person's change

# Review reasons the checks own: replaced, not added to, when the checks run again.
CHECK_REASON_PREFIXES: Final = ("check:", "check_error:", "check_failed:", "risk:")


class RecheckTrigger(StrEnum):
    CORRECTION = "correction"
    VENDOR_LINKED = "vendor_linked"
    CODING = "coding"
    PERSON = "person"


async def recheck_invoice(
    session: AsyncSession,
    invoice: PayInvoice,
    services: CheckServices,
    *,
    trigger: RecheckTrigger,
    by: By,
) -> list[str]:
    """Run every check and the risk again on ``invoice`` (locked by the caller), in the
    caller's transaction, after a person changed what the checks read.

    The same code as intake (``run_invoice_checks``), so a bank-detail correction that
    differs from the vendor master opens or joins the change ticket and holds the
    vendor exactly like the automatic check. The check reasons are replaced by the new
    ones. A ``ready`` invoice goes back to a person when a check now needs one. If the
    checks cannot run, the stored digest is cleared: the invoice stays stale, so it can
    never be approved or paid (fail closed). Returns the new check reasons.
    """
    before = invoice.status
    await session.flush()
    try:
        async with session.begin_nested():
            reasons = await run_invoice_checks(session, invoice, services)
    except Exception as exc:  # a check run that fails leaves the invoice stale
        log.error("pay.checks.recheck_failed", error=type(exc).__name__)
        await session.refresh(invoice)
        invoice.checks_digest = None
        reasons = [f"check_failed:{type(exc).__name__}"]
    kept = [r for r in invoice.review_reasons if not r.startswith(CHECK_REASON_PREFIXES)]
    invoice.review_reasons = kept
    add_reasons(invoice, reasons)
    if invoice.status == InvoiceStatus.READY.value and reasons:
        invoice.status = InvoiceStatus.NEEDS_REVIEW.value
    invoice.updated_at = datetime.now(UTC)
    await audit(
        session,
        by,
        "pay.invoice.rechecked",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "trigger": trigger.value,
            "previous_status": before,
            "status": invoice.status,
            "checks_current": invoice.checks_digest is not None,
            "review_reasons": reasons[:20],
        },
    )
    return reasons

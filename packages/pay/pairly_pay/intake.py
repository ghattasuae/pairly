# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Invoice intake (brief 4.2): the inbound email address, email handling, receipts.

Every way in goes through the engine's ingestion (sniffing, the intake policy, file
hash dedupe, storage, audit, then the virus scan before any parsing):

* **Email**: each organisation has one address for Pay,
  ``pay+<org-slug>.<token>@<PAIRLY_INBOUND_EMAIL_DOMAIN>`` (``pairly_core.ingestion
  .addresses``; the token is shown once when it is rotated). ``receive_email`` hands
  the raw message to ``ingest_email`` (routing, documents) and then decides, per
  ``pairly_pay.email_rules``:

  - marketing (high confidence): recorded as ignored, no invoice, no receipt;
  - each attachment that is an invoice becomes its own invoice; terms, logos and other
    supporting files stay with the email (and if only those came, they are read anyway,
    flagged for a person);
  - no attachment: the body, stored by the engine as a text document, is the invoice;
  - forwarded chains: the original sender's domain (from the forwarded block) is kept
    for vendor matching, next to the forwarder's;
  - unsure: the body becomes an invoice in ``needs_review`` (a person decides);
  - then a receipt with a reference number goes to the sender, under the engine's
    backscatter rules (``pairly_core.notifications.receipts``: never to bulk mail,
    autoresponders, role addresses or our own domains, rate limited).

* **Upload and API** (``pairly_pay.api.invoices``): the same document path, one file.

A file sent twice (any channel) is one document and so one invoice. Pay accepts the
engine's default types plus XML e-invoices unless ``PAIRLY_INTAKE_POLICIES`` sets a
``pay`` policy (``pay_policy``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import set_tenant
from pairly_core.ingestion.email import EmailIngestResult, ingest_email, parse_email
from pairly_core.ingestion.models import Document
from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy
from pairly_core.ingestion.sniff import TEXT, XML
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.notifications.receipts import ReceiptOutcome, reference_for, send_receipt
from pairly_core.notifications.settings import NotificationSettings
from pairly_pay.common import WORKFLOW, By
from pairly_pay.email_rules import EmailVerdict, classify_attachment, classify_email
from pairly_pay.invoices import create_invoice
from pairly_pay.matching import clean_domains
from pairly_pay.models import EmailClass, InvoiceSource, PayIntakeEmail

log = structlog.get_logger("pairly_pay.intake")

EMAIL_ACTOR: Final = "pairly.pay.intake.email"
BODY_FILENAME: Final = "email-body.txt"
WORKFLOWS: Final = frozenset({WORKFLOW})


def pay_policy(settings: IngestionSettings) -> IntakePolicy:
    """Pay's intake policy: the configured ``pay`` one, else the default plus XML."""
    configured = settings.intake_policies.get(WORKFLOW)
    if configured is not None:
        return configured
    return IntakePolicy(
        allowed_types=frozenset(settings.intake_default_types) | {XML},
        max_bytes=settings.intake_default_max_bytes,
    )


def pay_ingestion_settings(settings: IngestionSettings) -> IngestionSettings:
    """``settings`` with Pay's policy in force (for the engine's email ingestion)."""
    policies = {**settings.intake_policies, WORKFLOW: pay_policy(settings)}
    return settings.model_copy(update={"intake_policies": policies})


@dataclass
class EmailIntake:
    """What happened to one inbound email."""

    # ingested | duplicate | unroutable | too_large
    status: str
    org_id: uuid.UUID | None = None
    classification: str | None = None
    reasons: tuple[str, ...] = ()
    reference: str | None = None
    invoice_ids: list[uuid.UUID] = field(default_factory=list)
    # Invoices this email delivered again (the same file was already an invoice).
    known_invoice_ids: list[uuid.UUID] = field(default_factory=list)
    supporting_document_ids: list[uuid.UUID] = field(default_factory=list)
    # Documents created now (the worker scans them; the intake job starts their runs).
    created_document_ids: list[uuid.UUID] = field(default_factory=list)
    receipt: ReceiptOutcome | None = None


def _domain(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


async def _documents(
    session: AsyncSession, result: EmailIngestResult
) -> list[tuple[Document, str | None]]:
    """The documents this email delivered, with their file names, in order."""
    found: list[tuple[Document, str | None]] = []
    seen: set[uuid.UUID] = set()
    for item in result.items:
        if item.document_id is None or item.outcome not in {"created", "duplicate"}:
            continue
        if item.document_id in seen:
            continue
        doc = await session.scalar(select(Document).where(Document.id == item.document_id))
        if doc is not None and doc.workflow == WORKFLOW:
            seen.add(doc.id)
            found.append((doc, item.filename))
    if not found and result.inbound_email_id is not None:
        # A retried delivery: the engine knows the message, Pay may not have finished it.
        rows = await session.scalars(
            select(Document)
            .where(Document.inbound_email_id == result.inbound_email_id)
            .order_by(Document.created_at, Document.id)
        )
        found = [(doc, doc.original_filename) for doc in rows if doc.workflow == WORKFLOW]
    return found


async def _process(
    session: AsyncSession,
    result: EmailIngestResult,
    verdict: EmailVerdict,
    domains: tuple[str | None, str | None],
    outcome: EmailIntake,
) -> PayIntakeEmail:
    assert result.inbound_email_id is not None  # noqa: S101 - checked by the caller
    by = By.system(EMAIL_ACTOR)
    sender_domain, forwarded_domain = domains
    reference = reference_for(result.inbound_email_id)
    row = PayIntakeEmail(
        id=uuid.uuid4(),
        org_id=result.org_id,
        inbound_email_id=result.inbound_email_id,
        classification=verdict.kind,
        confidence=verdict.confidence,
        reasons=list(verdict.reasons),
        reference=reference,
        sender_domain=sender_domain,
        forwarded_domain=forwarded_domain,
        invoice_count=0,
        supporting_document_ids=[],
    )
    session.add(row)
    await session.flush()
    outcome.reference = reference
    if verdict.ignored:
        return row
    documents = await _documents(session, result)
    invoices: list[tuple[Document, list[str]]] = []
    supporting: list[Document] = []
    for doc, filename in documents:
        is_body = doc.media_type == TEXT and (filename or doc.original_filename) == BODY_FILENAME
        kind = (
            "invoice" if is_body else classify_attachment(filename, doc.media_type, doc.size_bytes)
        )
        if kind == "supporting":
            supporting.append(doc)
            continue
        reasons = []
        if verdict.kind == EmailClass.UNSURE.value:
            reasons.append("intake_unsure:" + ",".join(verdict.reasons))
        invoices.append((doc, reasons))
    if not invoices and supporting:
        # Only "supporting" files arrived: read them anyway and let a person decide.
        invoices = [(doc, ["intake_unsure:supporting_files_only"]) for doc in supporting]
        supporting = []
    # Forwarded first: the forwarder is usually our own staff.
    sender_domains = clean_domains([forwarded_domain, sender_domain])
    for doc, reasons in invoices:
        invoice, created = await create_invoice(
            session,
            document=doc,
            source=InvoiceSource.EMAIL,
            by=by,
            intake_email=row,
            sender_domains=sender_domains,
            review_reasons=reasons,
        )
        (outcome.invoice_ids if created else outcome.known_invoice_ids).append(invoice.id)
    row.invoice_count = len(outcome.invoice_ids)
    row.supporting_document_ids = [str(d.id) for d in supporting]
    outcome.supporting_document_ids = [d.id for d in supporting]
    return row


async def receive_email(
    raw: bytes,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: IngestionSettings,
    notification_settings: NotificationSettings,
    envelope_recipients: Sequence[str] = (),
    workdir: Path | None = None,
) -> EmailIntake:
    """Ingest one raw email for Pay, create its invoices and send the receipt.

    Unroutable and oversized messages are dropped without a bounce (the engine's rule).
    A failure after the engine stored the documents leaves them for a retry: a retried
    delivery is a duplicate for the engine, and Pay picks the email up from there.
    """
    parsed = parse_email(raw)
    verdict = classify_email(parsed)
    result = await ingest_email(
        raw,
        sessionmaker=sessionmaker,
        store=store,
        settings=pay_ingestion_settings(settings),
        workflows=WORKFLOWS,
        envelope_recipients=envelope_recipients,
        workdir=workdir,
    )
    outcome = EmailIntake(
        status=result.status,
        org_id=result.org_id,
        classification=verdict.kind,
        reasons=verdict.reasons,
        created_document_ids=list(result.created),
    )
    if result.org_id is None or result.inbound_email_id is None:
        return outcome
    forwarded = next((f.sender for f in parsed.forwarded if f.sender), None)
    domains = (parsed.sender_domain, _domain(forwarded))
    async with sessionmaker() as session:
        set_tenant(session, result.org_id, None)
        async with session.begin():
            known = await session.scalar(
                select(PayIntakeEmail).where(
                    PayIntakeEmail.inbound_email_id == result.inbound_email_id
                )
            )
            if known is not None:
                outcome.reference = known.reference
                return outcome
            row = await _process(session, result, verdict, domains, outcome)
            row_id = row.id
    log.info(
        "pay_email_received",
        classification=verdict.kind,
        invoices=len(outcome.invoice_ids),
        known=len(outcome.known_invoice_ids),
        supporting=len(outcome.supporting_document_ids),
    )
    if verdict.ignored or result.status != "ingested":
        return outcome
    try:
        outcome.receipt = await send_receipt(
            sessionmaker,
            raw,
            result,
            settings=notification_settings,
            ingestion=settings,
        )
    except Exception as exc:  # a receipt problem never fails intake
        log.warning("pay_receipt_failed", error=type(exc).__name__)
        outcome.receipt = ReceiptOutcome(False, "error")
    async with sessionmaker() as session:
        set_tenant(session, result.org_id, None)
        async with session.begin():
            stored = await session.scalar(select(PayIntakeEmail).where(PayIntakeEmail.id == row_id))
            if stored is not None:
                stored.receipt_sent = outcome.receipt.sent
                stored.receipt_reason = (outcome.receipt.reason or "")[:64] or None
    return outcome

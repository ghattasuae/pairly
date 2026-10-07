# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Receipt replies to inbound email (brief 4.2.3), with backscatter rules.

After ``ingest_email`` accepted a message, the sender gets one short reply with a
reference number (template ``core.ingest.receipt``). Sender addresses are trivially
forged, so a reply is only sent when every rule below holds; otherwise nothing is
sent and the reason is audited (``core.notification.receipt_skipped``):

1. The message was routed to an organisation and ingested now. Never for unroutable,
   oversized or duplicate messages: no reply can confirm that an address exists, and a
   retried delivery gets no second reply.
2. No ``Auto-Submitted`` header at all (RFC 3834 autoresponders, bounces, other systems).
3. Not bulk or list mail (``Precedence: bulk/list/junk``, ``List-Id``,
   ``List-Unsubscribe``), and no ``X-Auto-Response-Suppress`` asking for silence.
4. A real sender: an address in ``From``, not a null ``Return-Path`` (``<>``, bounces),
   not a role address that never reads mail (``mailer-daemon``, ``postmaster``,
   ``no-reply`` and friends, ``*-bounces``, ``owner-*``, ``*-request``), and not one of
   our own domains (no loops).
5. Rate limits: at most ``PAIRLY_RECEIPT_LIMIT_PER_SENDER_PER_DAY`` receipts per sender
   (by blind index, so no plaintext address is compared) and
   ``PAIRLY_RECEIPT_LIMIT_PER_ORG_PER_HOUR`` per organisation.

The reply goes to the ``From`` address, never to ``Reply-To`` (a forged ``Reply-To``
would turn us into a relay towards a third party), carries ``Auto-Submitted:
auto-replied`` and ``In-Reply-To``, and is idempotent by the inbound email id.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.service import db_now
from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.blind_index import blind_index
from pairly_core.db import set_tenant
from pairly_core.ingestion.email import EmailIngestResult, ingest_email
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.notifications.models import Notification
from pairly_core.notifications.service import (
    Extras,
    NotificationActions,
    Recipient,
    notify,
)
from pairly_core.notifications.settings import NotificationSettings

log = structlog.get_logger(__name__)

RECEIPT_TEMPLATE: Final = "core.ingest.receipt"
RECEIPT_ACTOR: Final = "pairly.notifications.receipts"
_ROLE_LOCALPARTS: Final = frozenset(
    {
        "mailer-daemon",
        "postmaster",
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "bounce",
        "bounces",
        "listserv",
        "majordomo",
        "abuse",
        "root",
    }
)
_ROLE_PATTERN: Final = re.compile(r"^(owner-.*|.*-request|.*-bounces?|bounce[s+-].*)$")
_HEADER_BYTES: Final = 64 * 1024


def reference_for(inbound_email_id: uuid.UUID) -> str:
    """A short, stable reference for one inbound email: ``REF-`` and 10 hex characters."""
    return f"REF-{inbound_email_id.hex[:10].upper()}"


@dataclass(frozen=True, slots=True)
class ReceiptHeaders:
    sender: str | None
    auto_submitted: bool
    bulk: bool
    suppress_autoreply: bool
    null_return_path: bool
    message_id: str | None
    subject: str | None


def read_headers(raw: bytes) -> ReceiptHeaders:
    """The headers the rules need. Only the header block is parsed."""
    head = raw[:_HEADER_BYTES]
    try:
        msg: Message = BytesParser(policy=policy.default).parsebytes(head, headersonly=True)
    except (ValueError, TypeError, RecursionError):
        return ReceiptHeaders(None, True, True, True, True, None, None)

    def get(name: str) -> str | None:
        try:
            value = msg.get(name)
        except Exception:
            return None
        return " ".join(str(value).split()) if value is not None else None

    _, sender = parseaddr(get("From") or "")
    precedence = (get("Precedence") or "").lower()
    return_path = get("Return-Path")
    suppress = (get("X-Auto-Response-Suppress") or "").lower()
    return ReceiptHeaders(
        sender=sender.strip().lower() if "@" in sender else None,
        auto_submitted=get("Auto-Submitted") is not None,
        bulk=bool(get("List-Id") or get("List-Unsubscribe"))
        or precedence in {"bulk", "list", "junk"},
        suppress_autoreply=any(token in suppress for token in ("all", "autoreply", "oof")),
        null_return_path=return_path is not None and return_path.strip() in {"<>", ""},
        message_id=(get("Message-ID") or "").strip("<> ") or None,
        subject=get("Subject"),
    )


def skip_reason(
    headers: ReceiptHeaders, result: EmailIngestResult, own_domains: Sequence[str]
) -> str | None:
    """Why no receipt may be sent, or None when the static rules allow one."""
    if result.status != "ingested" or result.org_id is None or result.inbound_email_id is None:
        return f"status_{result.status}"
    if headers.auto_submitted:
        return "auto_submitted"
    if headers.bulk:
        return "bulk"
    if headers.suppress_autoreply:
        return "autoreply_suppressed"
    if headers.null_return_path:
        return "null_return_path"
    if headers.sender is None:
        return "no_sender"
    local, _, domain = headers.sender.rpartition("@")
    if local in _ROLE_LOCALPARTS or _ROLE_PATTERN.match(local):
        return "role_address"
    domains = {d.lower().strip() for d in own_domains if d}
    if any(domain == d or domain.endswith(f".{d}") for d in domains):
        return "own_domain"
    return None


def _own_domains(settings: NotificationSettings, ingestion: IngestionSettings) -> list[str]:
    _, sender = parseaddr(settings.smtp_from)
    domains = [ingestion.inbound_email_domain]
    if "@" in sender:
        domains.append(sender.rsplit("@", 1)[1])
    return domains


async def _rate_limited(
    session: AsyncSession, settings: NotificationSettings, sender_bidx: str
) -> str | None:
    now = await db_now(session)
    per_sender = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.category == "receipts",
            Notification.recipient_bidx == sender_bidx,
            Notification.created_at > now - timedelta(days=1),
        )
    )
    if int(per_sender or 0) >= settings.receipt_limit_per_sender_per_day:
        return "rate_limited_sender"
    per_org = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.category == "receipts",
            Notification.created_at > now - timedelta(hours=1),
        )
    )
    if int(per_org or 0) >= settings.receipt_limit_per_org_per_hour:
        return "rate_limited_org"
    return None


@dataclass(frozen=True, slots=True)
class ReceiptOutcome:
    sent: bool
    reason: str | None
    reference: str | None = None
    notification_id: uuid.UUID | None = None


async def send_receipt(
    sessionmaker: async_sessionmaker[AsyncSession],
    raw: bytes,
    result: EmailIngestResult,
    *,
    settings: NotificationSettings,
    ingestion: IngestionSettings,
) -> ReceiptOutcome:
    """Queue a receipt reply for an ingested email when the rules allow it."""
    headers = read_headers(raw)
    reason = skip_reason(headers, result, _own_domains(settings, ingestion))
    if result.org_id is None or result.inbound_email_id is None:
        log.info("receipt_skipped", reason=reason)
        return ReceiptOutcome(False, reason)
    reference = reference_for(result.inbound_email_id)
    async with sessionmaker() as session:
        set_tenant(session, result.org_id, None)
        async with session.begin():
            if reason is None and headers.sender is not None:
                # Serialise per organisation, so concurrent ingests cannot overshoot limits.
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(f"receipts:{result.org_id}", 0)
                        )
                    )
                )
                sender_bidx = blind_index("email", headers.sender, result.org_id)
                reason = (
                    await _rate_limited(session, settings, sender_bidx)
                    if sender_bidx
                    else "no_sender"
                )
            if reason is not None or headers.sender is None:
                await append_audit(
                    session,
                    action=NotificationActions.RECEIPT_SKIPPED,
                    actor_type=ActorType.SYSTEM,
                    actor_id=RECEIPT_ACTOR,
                    entity_type="inbound_email",
                    entity_id=result.inbound_email_id,
                    workflow=result.workflow,
                    payload={"reason": reason or "no_sender"},
                )
                return ReceiptOutcome(False, reason or "no_sender", reference)
            documents = sum(1 for item in result.items if item.outcome in {"created", "duplicate"})
            results = await notify(
                session,
                recipient=Recipient.external(headers.sender),
                template_key=RECEIPT_TEMPLATE,
                variables={
                    "reference": reference,
                    "original_subject": (headers.subject or "")[:200] or None,
                    "received_at": await db_now(session),
                    "document_count": documents,
                },
                dedupe_key=f"receipt:{result.inbound_email_id}",
                channels=["email"],
                extras=Extras(in_reply_to=headers.message_id, auto_submitted="auto-replied"),
                workflow=result.workflow,
                actor_id=RECEIPT_ACTOR,
            )
    first = results[0] if results else None
    sent = first is not None and first.status == "queued"
    return ReceiptOutcome(
        sent, None if sent else "not_queued", reference, first.notification_id if first else None
    )


async def ingest_email_with_receipt(
    raw: bytes,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: IngestionSettings,
    notification_settings: NotificationSettings,
    workflows: frozenset[str],
    envelope_recipients: Sequence[str] = (),
    workdir: Path | None = None,
) -> tuple[EmailIngestResult, ReceiptOutcome]:
    """``ingest_email`` followed by ``send_receipt``. Receipt problems never fail intake."""
    result = await ingest_email(
        raw,
        sessionmaker=sessionmaker,
        store=store,
        settings=settings,
        workflows=workflows,
        envelope_recipients=envelope_recipients,
        workdir=workdir,
    )
    try:
        outcome = await send_receipt(
            sessionmaker, raw, result, settings=notification_settings, ingestion=settings
        )
    except Exception as exc:
        log.warning("receipt_failed", error=type(exc).__name__)
        outcome = ReceiptOutcome(False, "error")
    return result, outcome

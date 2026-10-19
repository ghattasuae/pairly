# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pay's intake address on Resend inbound email.

Resend receives mail for the inbound domain (an MX record pointing at Resend) and calls
``POST /v1/pay/inbound/resend`` (``pairly_pay.api.inbound``) with a signed
``email.received`` event. The route verifies the signature
(``pairly_core.integrations.resend.signature``) and hands the event to
``handle_received`` here, which:

1. Routes on the event's recipients (``to`` and ``received_for``), with the same rules as
   every other inbound email (``candidate_routes`` and ``lookup_inbound_org``): an
   unknown address, organisation or token is dropped without a bounce and without
   fetching anything.
2. Fetches the message's raw MIME from Resend's Receiving API
   (``pairly_core.integrations.resend.receiving``), within ``max_email_bytes``.
3. Records the event (``inbound_events``, provider ``resend``, key ``svix-id``) in the
   organisation and, in the same open transaction, runs ``pairly_pay.intake.receive_email``
   exactly as the development drop does (attachments stored and scanned, invoices
   created, receipt sent). The record commits only after intake succeeded, so a failure
   answers non-2xx and Resend's retry is processed, while a retry of a processed event,
   or a replay inside the signature window, is a duplicate. Intake is also idempotent on
   the email's Message-ID, so a concurrent duplicate cannot create invoices twice.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import set_tenant
from pairly_core.ingestion.addresses import candidate_routes, lookup_inbound_org
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.integrations.resend.receiving import valid_email_id
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.webhooks.inbound import record_inbound_event
from pairly_pay.intake import WORKFLOWS, receive_email

log = structlog.get_logger("pairly_pay.resend_inbound")

PROVIDER: Final = "resend"
EVENT_TYPE: Final = "email.received"
MAX_RECIPIENTS: Final = 50

FetchRaw = Callable[[str, int], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class ReceivedEvent:
    msg_id: str
    email_id: str
    recipients: tuple[str, ...]


def _addresses(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [a.strip() for a in value if isinstance(a, str) and 0 < len(a.strip()) <= 320]


def parse_event(msg_id: str, body: bytes) -> ReceivedEvent | None:
    """The ``email.received`` event in ``body``, or None for anything else (ignored)."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != EVENT_TYPE:
        return None
    inner = data.get("data")
    if not isinstance(inner, dict) or not valid_email_id(inner.get("email_id")):
        return None
    recipients: list[str] = []
    for address in [*_addresses(inner.get("to")), *_addresses(inner.get("received_for"))]:
        if address.lower() not in (r.lower() for r in recipients):
            recipients.append(address)
    return ReceivedEvent(msg_id, str(inner["email_id"]), tuple(recipients[:MAX_RECIPIENTS]))


async def route_org(
    sessionmaker: async_sessionmaker[AsyncSession],
    recipients: tuple[str, ...],
    settings: IngestionSettings,
) -> uuid.UUID | None:
    for candidate in candidate_routes(list(recipients), settings.inbound_email_domain, WORKFLOWS):
        org_id = await lookup_inbound_org(sessionmaker, candidate)
        if org_id is not None:
            return org_id
    return None


async def handle_received(
    event: ReceivedEvent,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    ingestion: IngestionSettings,
    notifications: NotificationSettings,
    fetch_raw: FetchRaw,
) -> str:
    """Process one event. Returns ``unroutable``, ``duplicate`` or the intake status.

    Raises when processing failed (the caller answers non-2xx so Resend retries).
    """
    org_id = await route_org(sessionmaker, event.recipients, ingestion)
    if org_id is None:
        # Dropped, never bounced, nothing fetched. No address or token is logged.
        log.warning("pay_resend_unroutable", recipients=len(event.recipients))
        return "unroutable"
    raw = await fetch_raw(event.email_id, ingestion.max_email_bytes)
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            if not await record_inbound_event(session, provider=PROVIDER, event_id=event.msg_id):
                log.info("pay_resend_duplicate", org_id=str(org_id))
                return "duplicate"
            outcome = await receive_email(
                raw,
                sessionmaker=sessionmaker,
                store=store,
                settings=ingestion,
                notification_settings=notifications,
                envelope_recipients=event.recipients,
            )
    log.info(
        "pay_resend_received",
        org_id=str(org_id),
        status=outcome.status,
        invoices=len(outcome.invoice_ids),
    )
    return outcome.status

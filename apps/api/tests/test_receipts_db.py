# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Receipt replies to inbound email against Postgres (WP1.9, brief 4.2.3).

The full path: ``ingest_email`` stores the message, then ``send_receipt`` queues a reply
only when the backscatter rules allow it, and the delivery job sends it (to a fake
transport). Every refusal is audited and never blocks intake."""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType
from pairly_core.config import Environment
from pairly_core.crypto.blind_index import blind_index
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.ingestion.addresses import inbound_address, rotate_inbound_token
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.email import FakeEmailTransport
from pairly_core.notifications.receipts import (
    ReceiptOutcome,
    ingest_email_with_receipt,
    reference_for,
)
from pairly_core.notifications.settings import NotificationSettings

from .notify_support import notifications, notify_settings
from .wf_support import World, audit_actions, owner_sql, wf_engine, world

__all__ = ["wf_engine", "world"]  # fixtures

DOMAIN = "inbound.pairly.localhost"
WORKFLOWS = frozenset({"hire", "pay"})
SENDER = "billing@vendor.example"


def _ingestion() -> IngestionSettings:
    return IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain=DOMAIN,
        parse_memory_mb=768,
        ocr_languages="eng",
    )


async def _address(owner: AsyncEngine, world: World) -> str:
    (org,) = await owner_sql(owner, world.org_id, "SELECT slug FROM organisations")
    session = create_sessionmaker(owner)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        token = await rotate_inbound_token(
            session, workflow="pay", actor_type=ActorType.SYSTEM, actor_id="pairly.test"
        )
    return inbound_address("pay", org.slug, token, DOMAIN)


def _mail(
    to: str,
    *,
    sender: str = f"Billing <{SENDER}>",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> tuple[bytes, str]:
    message_id = f"m-{uuid.uuid4().hex}@vendor.example"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = "Invoice 77"
    msg["Message-ID"] = f"<{message_id}>"
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body or f"Invoice {uuid.uuid4().hex[:6]}, total 900 AED, due in 30 days.")
    return msg.as_bytes(policy=SMTP), message_id


class Intake:
    def __init__(
        self, maker: async_sessionmaker[AsyncSession], settings: NotificationSettings
    ) -> None:
        self.maker = maker
        self.settings = settings
        self.store = InMemoryObjectStore()
        self.ingestion = _ingestion()

    async def __call__(self, raw: bytes, **kw: Any) -> tuple[str, ReceiptOutcome]:
        result, outcome = await ingest_email_with_receipt(
            raw,
            sessionmaker=self.maker,
            store=self.store,
            settings=self.ingestion,
            notification_settings=self.settings,
            workflows=WORKFLOWS,
            **kw,
        )
        return result.status, outcome


async def test_receipt_reply_for_routed_mail(
    world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    maker = create_sessionmaker(wf_engine)
    settings = notify_settings()
    intake = Intake(maker, settings)
    address = await _address(owner_engine, world)
    raw, message_id = _mail(
        f"Accounts <{address}>", headers={"Reply-To": "Someone Else <relay@elsewhere.example>"}
    )

    status, outcome = await intake(raw)
    assert status == "ingested"
    assert outcome.sent
    assert outcome.reason is None
    assert outcome.reference is not None
    assert outcome.reference.startswith("REF-")
    ((row, content),) = await notifications(maker, world.org_id, category="receipts")
    assert row.id == outcome.notification_id
    assert (row.template_key, row.status, row.recipient_user_id) == (
        "core.ingest.receipt",
        "queued",
        None,
    )
    assert row.recipient_bidx == blind_index("email", SENDER, world.org_id)
    assert outcome.reference in content["text"]
    assert content["auto_submitted"] == "auto-replied"
    assert content["in_reply_to"] == message_id
    assert content["list_unsubscribe"] is False

    # A retried delivery of the same message gets no second reply.
    again_status, again = await intake(raw)
    assert (again_status, again.sent, again.reason) == ("duplicate", False, "status_duplicate")
    assert len(await notifications(maker, world.org_id, category="receipts")) == 1

    fake = FakeEmailTransport()
    transports = Transports(settings=settings, email=fake, slack=FakeSlackTransport())
    assert (await deliver_org(maker, world.org_id, transports)).sent == 1
    (email,) = fake.sent
    (message,) = fake.messages
    # To the From address, never to Reply-To.
    assert email.to == SENDER
    assert "elsewhere.example" not in str(message["To"])
    assert message["Auto-Submitted"] == "auto-replied"
    assert message["In-Reply-To"] == f"<{message_id}>"
    assert message["List-Unsubscribe"] is None
    (inbound,) = await owner_sql(
        owner_engine, world.org_id, "SELECT id FROM inbound_emails WHERE id IS NOT NULL"
    )
    assert outcome.reference == reference_for(inbound.id)


async def test_no_receipt_where_a_reply_could_cause_backscatter(
    world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    maker = create_sessionmaker(wf_engine)
    intake = Intake(maker, notify_settings())
    address = await _address(owner_engine, world)
    cases: list[tuple[dict[str, Any], str]] = [
        ({"headers": {"Auto-Submitted": "auto-replied"}}, "auto_submitted"),
        # Strict: any Auto-Submitted header, even "no", means no receipt.
        ({"headers": {"Auto-Submitted": "no"}}, "auto_submitted"),
        ({"headers": {"Precedence": "bulk"}}, "bulk"),
        ({"headers": {"Precedence": "list"}}, "bulk"),
        ({"headers": {"List-Id": "Vendor news <news.vendor.example>"}}, "bulk"),
        ({"headers": {"List-Unsubscribe": "<mailto:leave@vendor.example>"}}, "bulk"),
        ({"headers": {"X-Auto-Response-Suppress": "All"}}, "autoreply_suppressed"),
        ({"headers": {"X-Auto-Response-Suppress": "OOF, AutoReply"}}, "autoreply_suppressed"),
        ({"headers": {"Return-Path": "<>"}}, "null_return_path"),
        ({"sender": "MAILER-DAEMON@vendor.example"}, "role_address"),
        ({"sender": "No-Reply <no-reply@vendor.example>"}, "role_address"),
        ({"sender": "postmaster@vendor.example"}, "role_address"),
        ({"sender": "invoices-bounces@vendor.example"}, "role_address"),
        ({"sender": "owner-ap@vendor.example"}, "role_address"),
        ({"sender": f"loop@{DOMAIN}"}, "own_domain"),
        ({"sender": "someone@pairly.localhost"}, "own_domain"),
        ({"sender": "someone@mail.pairly.localhost"}, "own_domain"),
        ({"sender": "undisclosed-recipients:;"}, "no_sender"),
    ]
    for kw, reason in cases:
        raw, _ = _mail(address, **kw)
        status, outcome = await intake(raw)
        # Intake is never blocked: only the reply is skipped.
        assert status == "ingested", (kw, status)
        assert (outcome.sent, outcome.reason) == (False, reason), kw
    assert await notifications(maker, world.org_id, category="receipts") == []
    actions = await audit_actions(owner_engine, world.org_id)
    assert actions.count("core.notification.receipt_skipped") == len(cases)

    # Unroutable mail: nothing is stored, nobody is told, and the organisation sees nothing.
    raw, _ = _mail(f"pay+no-such-org.{'a' * 20}@{DOMAIN}")
    status, outcome = await intake(raw)
    assert (status, outcome.sent, outcome.reason) == ("unroutable", False, "status_unroutable")
    raw, _ = _mail("someone@example.test")
    assert (await intake(raw))[0] == "unroutable"
    after = await audit_actions(owner_engine, world.org_id)
    assert after.count("core.notification.receipt_skipped") == len(cases)


async def test_receipt_rate_limits_per_sender_and_per_organisation(
    world: World, owner_engine: AsyncEngine, wf_engine: AsyncEngine
) -> None:
    maker = create_sessionmaker(wf_engine)
    intake = Intake(
        maker,
        notify_settings(receipt_limit_per_sender_per_day=2, receipt_limit_per_org_per_hour=4),
    )
    address = await _address(owner_engine, world)

    async def send(sender: str) -> ReceiptOutcome:
        raw, _ = _mail(address, sender=sender)
        status, outcome = await intake(raw)
        assert status == "ingested"
        return outcome

    # Per sender, by blind index: the case of the address does not matter.
    assert (await send(SENDER)).sent
    assert (await send("Billing <Billing@Vendor.Example>")).sent
    limited = await send(SENDER)
    assert (limited.sent, limited.reason) == (False, "rate_limited_sender")
    # Per organisation.
    assert (await send("ap@second.example")).sent
    assert (await send("ap@third.example")).sent
    limited = await send("ap@fourth.example")
    assert (limited.sent, limited.reason) == (False, "rate_limited_org")
    rows = await notifications(maker, world.org_id, category="receipts")
    assert len(rows) == 4
    assert sum(r.recipient_bidx == blind_index("email", SENDER, world.org_id) for r, _ in rows) == 2
    skipped = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT payload->>'reason' AS reason FROM audit_events "
        "WHERE action = 'core.notification.receipt_skipped' ORDER BY seq",
    )
    assert [r.reason for r in skipped] == ["rate_limited_sender", "rate_limited_org"]

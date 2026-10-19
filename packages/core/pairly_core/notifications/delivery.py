# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Delivery (worker side): send queued email and Slack messages, build daily digests.

``deliver_org`` takes due rows one at a time (``FOR UPDATE SKIP LOCKED``, so several
workers never take the same row), sends, and records the outcome in the same short
transaction:

* success: ``sent``, provider message id, ``core.notification.sent``;
* transient failure: ``attempts`` + 1 and ``next_attempt_at`` pushed back exponentially
  (``PAIRLY_DELIVERY_BACKOFF_SECONDS`` doubled per attempt, at most one hour),
  ``core.notification.retry_scheduled``; after ``PAIRLY_DELIVERY_MAX_ATTEMPTS`` it
  becomes ``failed``;
* permanent failure: ``failed`` at once, ``core.notification.failed``.

Idempotency: rows are created once per dedupe key, and only ``queued`` rows are sent, so
the same notification is never sent twice by us. If the process dies after the provider
accepted a message but before the commit, the retry reuses the same ``Message-ID`` and
the same Resend ``Idempotency-Key``, so the provider or the receiver drops the copy.

``build_digests`` groups each person's held (``digest``) items into one
``core.digest.daily`` email per day (dedupe key ``digest:<user>:<date>``) and marks the
items as delivered through it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.service import db_now
from pairly_core.audit import ActorType
from pairly_core.crypto.types import unseal
from pairly_core.db import set_tenant
from pairly_core.integrations.slack.links import active_slack, bot_token
from pairly_core.integrations.slack.transport import SlackTransport
from pairly_core.notifications.email import (
    Attachment,
    DeliveryError,
    EmailTransport,
    OutgoingEmail,
)
from pairly_core.notifications.links import absolute_url, is_app_path, public_web_url
from pairly_core.notifications.models import Notification, NotificationStatus
from pairly_core.notifications.service import (
    NOTIFY_ACTOR,
    NotificationActions,
    Recipient,
    audit_notification,
    notify,
)
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.notifications.unsubscribe import UnsubscribeClaim, make_token

log = structlog.get_logger(__name__)

DELIVERY_ACTOR = "pairly.notifications.delivery"
MAX_BACKOFF_SECONDS = 3600
MAX_DIGEST_ITEMS = 100


@dataclass
class Transports:
    settings: NotificationSettings
    email: EmailTransport
    slack: SlackTransport


@dataclass
class DeliveryStats:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def backoff(settings: NotificationSettings, attempts: int) -> timedelta:
    seconds = settings.delivery_backoff_seconds * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, MAX_BACKOFF_SECONDS))


def _from_address(settings: NotificationSettings) -> str:
    return settings.smtp_from


def _message_domain(settings: NotificationSettings) -> str:
    sender = settings.smtp_from
    domain = sender.rsplit("@", 1)[-1].strip(" >") if "@" in sender else "pairly.localhost"
    return domain or "pairly.localhost"


def unsubscribe_url(settings: NotificationSettings, row: Notification) -> str | None:
    if settings.signed_link_secret is None or row.recipient_user_id is None:
        return None
    token = make_token(
        settings.signed_link_secret.get_secret_value(),
        UnsubscribeClaim(row.org_id, row.recipient_user_id, row.category),
    )
    return f"{settings.api_url}/v1/notifications/unsubscribe?token={token}"


def _attachments(raw: Any) -> tuple[Attachment, ...]:
    if not isinstance(raw, list):
        return ()
    found = (Attachment.from_json(item) for item in raw)
    return tuple(a for a in found if a is not None)


async def _send(transports: Transports, session: AsyncSession, row: Notification) -> str:
    content: dict[str, Any] = unseal(row, "content") or {}
    address = unseal(row, "recipient_address")
    if not isinstance(address, str) or not address:
        raise DeliveryError("no_address", transient=False)
    if row.channel == "email":
        settings = transports.settings
        email = OutgoingEmail(
            to=address,
            sender=_from_address(settings),
            subject=str(content.get("subject") or content.get("title") or ""),
            text=str(content.get("text") or ""),
            html=content.get("html"),
            locale=row.locale,
            message_id=f"{row.id}@{_message_domain(settings)}",
            idempotency_key=f"{row.org_id}:{row.dedupe_key}",
            list_unsubscribe=unsubscribe_url(settings, row)
            if content.get("list_unsubscribe")
            else None,
            auto_submitted=str(content.get("auto_submitted") or "auto-generated"),
            in_reply_to=content.get("in_reply_to"),
            attachments=_attachments(content.get("attachments")),
            reply_to=settings.email_reply_to,
            tags=(("category", row.category),),
        )
        return await transports.email.send(email)
    if row.channel == "slack":
        integration = await active_slack(session)
        if integration is None:
            raise DeliveryError("slack_not_configured", transient=False)
        blocks = content.get("blocks")
        return await transports.slack.post_message(
            token=bot_token(integration),
            channel=address,
            text=str(content.get("text") or ""),
            blocks=blocks if isinstance(blocks, list) else [],
        )
    raise DeliveryError("channel_not_deliverable", transient=False)


async def deliver_one(
    session: AsyncSession, transports: Transports, *, now: datetime | None = None
) -> Notification | None:
    """Send the next due row of the session's organisation. None when nothing is due."""
    now = now or await db_now(session)
    row = await session.scalar(
        select(Notification)
        .where(
            Notification.status == NotificationStatus.QUEUED.value,
            Notification.channel.in_(("email", "slack")),
            Notification.next_attempt_at <= now,
        )
        .order_by(Notification.next_attempt_at, Notification.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    settings = transports.settings
    try:
        provider_id = await _send(transports, session, row)
    except DeliveryError as exc:
        row.attempts += 1
        row.reason = exc.code[:64]
        if exc.transient and row.attempts < settings.delivery_max_attempts:
            wait = backoff(settings, row.attempts)
            if exc.retry_after is not None:
                # The provider said when to come back (Resend's Retry-After): never earlier.
                wait = max(wait, timedelta(seconds=min(exc.retry_after, MAX_BACKOFF_SECONDS)))
            row.next_attempt_at = now + wait
            action = NotificationActions.RETRY
        else:
            row.status = NotificationStatus.FAILED.value
            row.next_attempt_at = None
            action = NotificationActions.FAILED
        await session.flush()
        await audit_notification(
            session, action, row, actor_type=ActorType.SYSTEM, actor_id=DELIVERY_ACTOR
        )
        log.warning(
            "notification_delivery_failed",
            notification_id=str(row.id),
            channel=row.channel,
            code=exc.code,
            transient=exc.transient,
            attempts=row.attempts,
        )
        return row
    row.attempts += 1
    row.status = NotificationStatus.SENT.value
    row.reason = None
    row.sent_at = now
    row.next_attempt_at = None
    row.provider_message_id = provider_id[:256]
    await session.flush()
    await audit_notification(
        session, NotificationActions.SENT, row, actor_type=ActorType.SYSTEM, actor_id=DELIVERY_ACTOR
    )
    log.info("notification_sent", notification_id=str(row.id), channel=row.channel)
    return row


async def deliver_org(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    transports: Transports,
    *,
    limit: int = 200,
) -> DeliveryStats:
    """Deliver up to ``limit`` due rows of one organisation, one transaction each."""
    stats = DeliveryStats()
    for _ in range(limit):
        async with sessionmaker() as session:
            set_tenant(session, org_id, None)
            async with session.begin():
                row = await deliver_one(session, transports)
        if row is None:
            break
        if row.status == NotificationStatus.SENT.value:
            stats.sent += 1
        elif row.status == NotificationStatus.FAILED.value:
            stats.failed += 1
        else:
            stats.retried += 1
    return stats


@dataclass
class DigestStats:
    digests: int = 0
    items: int = 0
    # People whose digest failed (rolled back to their savepoint, items kept held).
    failed: int = 0


def _digest_url(link: object) -> str:
    """The full link of an item in the digest email (items store an app path)."""
    if not isinstance(link, str) or not link:
        return ""
    if link.startswith(("https://", "http://")):
        return link  # an item stored before links became app paths
    if not is_app_path(link):
        return ""
    return absolute_url(public_web_url(), link) or ""


async def _digest_for(
    session: AsyncSession,
    user_id: uuid.UUID,
    items: list[Notification],
    *,
    day: str,
    now: datetime,
) -> int:
    """Queue ``user_id``'s digest of ``items``. Returns how many items it covers."""
    lines: list[dict[str, str]] = []
    for row in items[:MAX_DIGEST_ITEMS]:
        content = unseal(row, "content") or {}
        entry = content.get("digest") if isinstance(content, dict) else None
        if isinstance(entry, dict):
            # Always both keys: the digest template reads ``item.url`` under strict
            # undefined, so an item without a link must carry an empty one.
            lines.append(
                {
                    "title": str(entry.get("title") or ""),
                    "url": _digest_url(entry.get("url")),
                }
            )
    results = await notify(
        session,
        recipient=Recipient.user(user_id),
        template_key="core.digest.daily",
        variables={"items": lines, "count": len(items), "date": day},
        dedupe_key=f"digest:{user_id}:{day}",
        channels=["email"],
        actor_id=NOTIFY_ACTOR,
    )
    result = results[0] if results else None
    if result is None or result.replayed or result.notification_id is None:
        return 0  # today's digest exists already: these items wait for tomorrow's
    delivered = result.status != NotificationStatus.SUPPRESSED.value
    for row in items:
        row.status = (
            NotificationStatus.SENT.value if delivered else NotificationStatus.SUPPRESSED.value
        )
        row.reason = "digest" if delivered else "digest_opted_out"
        row.digest_id = result.notification_id
        row.sent_at = now if delivered else None
    await session.flush()
    return len(items)


async def build_digests(session: AsyncSession, *, now: datetime | None = None) -> DigestStats:
    """Queue one digest email per person with held items (the session's organisation)."""
    now = now or await db_now(session)
    rows = list(
        await session.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.DIGEST.value,
                Notification.recipient_user_id.is_not(None),
            )
            .order_by(Notification.recipient_user_id, Notification.created_at)
            .with_for_update(skip_locked=True)
        )
    )
    by_user: dict[uuid.UUID, list[Notification]] = defaultdict(list)
    for row in rows:
        if row.recipient_user_id is not None:
            by_user[row.recipient_user_id].append(row)
    stats = DigestStats()
    day = now.date().isoformat()
    for user_id, items in by_user.items():
        # One savepoint per person: a bad item (undecryptable content, a template that
        # fails to render for this person) rolls back only that person's digest, logs,
        # and the rest of the organisation's digests still go out. The person's items
        # stay held and are tried again with the next digest.
        try:
            async with session.begin_nested():
                count = await _digest_for(session, user_id, items, day=day, now=now)
        except Exception as exc:
            stats.failed += 1
            log.warning("digest_person_failed", user_id=str(user_id), error=type(exc).__name__)
            continue
        if count:
            stats.digests += 1
            stats.items += count
    return stats

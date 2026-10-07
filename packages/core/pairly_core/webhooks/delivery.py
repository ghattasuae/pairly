# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Outbound webhook delivery (the worker side).

Per organisation (found through ``webhook_due_org_ids()``, SECURITY DEFINER):

1. ``fan_out``: every undispatched ``webhook_outbox`` row becomes one
   ``webhook_deliveries`` row per active endpoint subscribed to its type (unique per
   endpoint and event, so a repeated fan-out never doubles a delivery); the outbox row
   gets ``dispatched_at``. ``FOR UPDATE SKIP LOCKED`` lets several workers share the work.
2. ``deliver_due``: claims due deliveries (``locked_until`` protects them while the HTTP
   call runs, so a crashed worker's claim simply expires), sends each one, then records
   the outcome.

Sending: ``HttpxSender`` re-checks the URL against the SSRF rules (``ssrf``) and pins
the connection to the checked address; POST with the canonical JSON body and the
headers ``Pairly-Signature`` (``t=...,v1=...``), ``Pairly-Event-Id``,
``Pairly-Event-Type``, ``Pairly-Delivery-Id`` and ``Pairly-Delivery-Attempt``. Any 2xx is
success; anything else (3xx included, redirects are never followed), a timeout or a
refused URL is a failure.

Retries: exponential backoff (``RETRY_SCHEDULE``, about 1 minute up to 12 hours, with
jitter), for at most 24 hours from the event; then the delivery is ``failed``.

Auto-disable: every failed attempt adds one to the endpoint's ``consecutive_failures``
(a success resets it). At ``PAIRLY_WEBHOOK_MAX_CONSECUTIVE_FAILURES`` the endpoint is
disabled, its pending deliveries are cancelled, an audit event
``core.webhook.endpoint_disabled`` is written and the ``on_endpoint_disabled`` hook runs
(notifications wire into it), all in one transaction.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import httpx
import structlog
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import unseal
from pairly_core.db import set_tenant
from pairly_core.webhooks.events import envelope, event_ref, serialise
from pairly_core.webhooks.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookOutbox,
)
from pairly_core.webhooks.settings import WebhookSettings
from pairly_core.webhooks.signature import SIGNATURE_HEADER, sign_payload
from pairly_core.webhooks.ssrf import (
    Resolver,
    UnsafeWebhookUrlError,
    parse_target,
    resolve_target,
    system_resolver,
)

log = structlog.get_logger("pairly_core.webhooks.delivery")

# Seconds to wait after the 1st, 2nd, ... failed attempt. 8 attempts in about 22 hours.
RETRY_SCHEDULE: Final = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600)
MAX_AGE: Final = timedelta(hours=24)
JITTER: Final = 0.1
SYSTEM_ACTOR: Final = "pairly.webhooks"
DISABLED_ACTION: Final = "core.webhook.endpoint_disabled"
FAN_OUT_BATCH: Final = 500
USER_AGENT: Final = "Pairly-Webhooks/1"


def retry_delay(failed_attempts: int, *, rng: Callable[[], float] = random.random) -> float | None:
    """Seconds before the next attempt after ``failed_attempts`` failures, or None (give up).

    Jitter of +-10% spreads retries of many deliveries that failed together.
    """
    if failed_attempts < 1 or failed_attempts > len(RETRY_SCHEDULE):
        return None
    base = RETRY_SCHEDULE[failed_attempts - 1]
    return float(base * (1 + JITTER * (2 * rng() - 1)))


@dataclass(frozen=True, slots=True)
class OutgoingWebhook:
    delivery_id: uuid.UUID
    url: str
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class SendResult:
    status_code: int | None
    latency_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300


class WebhookSender(Protocol):
    async def send(self, webhook: OutgoingWebhook) -> SendResult: ...


class HttpxSender:
    """Sends over HTTPS to the SSRF-checked address, never following redirects."""

    def __init__(
        self,
        settings: WebhookSettings,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                settings.webhook_timeout_seconds, connect=settings.webhook_connect_timeout_seconds
            ),
            limits=httpx.Limits(max_connections=settings.webhook_concurrency * 2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, webhook: OutgoingWebhook) -> SendResult:
        start = self._clock()

        def elapsed() -> int:
            return max(int((self._clock() - start) * 1000), 0)

        try:
            target = parse_target(webhook.url, allow_http=self._settings.webhook_allow_http)
            ip = await resolve_target(
                target, self._resolver, allow_private=self._settings.webhook_allow_private_targets
            )
        except UnsafeWebhookUrlError:
            return SendResult(None, elapsed(), "unsafe_url")
        headers = dict(webhook.headers) | {"Host": target.host_header}
        extensions: dict[str, Any] = {}
        if target.scheme == "https":
            # Connect to the checked IP, but present and verify the original name (SNI).
            extensions["sni_hostname"] = target.host
        try:
            async with asyncio.timeout(self._settings.webhook_timeout_seconds + 1):
                async with self._client.stream(
                    "POST",
                    target.url_for(ip),
                    content=webhook.body,
                    headers=headers,
                    extensions=extensions,
                ) as response:
                    read = 0
                    async for chunk in response.aiter_raw():
                        read += len(chunk)
                        if read >= self._settings.webhook_max_response_bytes:
                            break  # the body is not needed; stop reading it
                    return SendResult(response.status_code, elapsed())
        except (TimeoutError, httpx.TimeoutException):
            return SendResult(None, elapsed(), "timeout")
        except httpx.HTTPError as exc:
            return SendResult(None, elapsed(), type(exc).__name__[:64])


async def _db_now(session: AsyncSession) -> datetime:
    now: datetime | None = await session.scalar(select(func.now()))
    if now is None:  # pragma: no cover - now() always returns a value
        raise RuntimeError("the database returned no time")
    return now


EndpointDisabledHook = Callable[[AsyncSession, WebhookEndpoint], Awaitable[None]]


async def _no_hook(_session: AsyncSession, _endpoint: WebhookEndpoint) -> None:
    return None


@dataclass
class DeliveryStats:
    fanned_out: int = 0
    sent: int = 0
    succeeded: int = 0
    failed_attempts: int = 0
    gave_up: int = 0
    disabled_endpoints: list[str] = field(default_factory=list)
    skipped: bool = False  # the organisation's webhooks.outbound flag is off


@dataclass(frozen=True, slots=True)
class _Claimed:
    delivery_id: uuid.UUID
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    attempt: int
    created_at: datetime
    url: str
    secret: str
    body: bytes


class WebhookDispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sender: WebhookSender,
        settings: WebhookSettings,
        *,
        on_endpoint_disabled: EndpointDisabledHook = _no_hook,
        enabled_for: Callable[[AsyncSession], Awaitable[bool]] | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._sender = sender
        self._settings = settings
        self._on_disabled = on_endpoint_disabled
        self._enabled_for = enabled_for
        self._rng = rng

    def _session(self, org_id: uuid.UUID) -> AsyncSession:
        session = self._sessionmaker()
        set_tenant(session, org_id, None)
        return session

    # ------------------------------------------------------------------ fan out

    async def fan_out(self, org_id: uuid.UUID) -> int:
        """Turn undispatched outbox events into deliveries. Returns deliveries created."""
        created = 0
        async with self._session(org_id) as session, session.begin():
            events = list(
                await session.scalars(
                    select(WebhookOutbox)
                    .where(WebhookOutbox.dispatched_at.is_(None))
                    .order_by(WebhookOutbox.created_at, WebhookOutbox.id)
                    .limit(FAN_OUT_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            if not events:
                return 0
            endpoints = list(
                await session.scalars(select(WebhookEndpoint).where(WebhookEndpoint.active))
            )
            for event in events:
                payload = envelope(
                    event_id=event.id,
                    event_type=event.event_type,
                    created_at=event.created_at,
                    org_id=org_id,
                    data=event.data,
                )
                rows = [
                    {
                        "org_id": org_id,
                        "endpoint_id": endpoint.id,
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "payload": payload,
                        "created_at": event.created_at,
                    }
                    for endpoint in endpoints
                    if event.event_type in endpoint.event_types
                ]
                if rows:
                    result: Any = await session.execute(
                        insert(WebhookDelivery)
                        .values(rows)
                        .on_conflict_do_nothing(constraint="uq_webhook_deliveries_endpoint_event")
                    )
                    created += int(result.rowcount or 0)
            await session.execute(
                update(WebhookOutbox)
                .where(WebhookOutbox.id.in_([e.id for e in events]))
                .values(dispatched_at=func.now())
                .execution_options(synchronize_session=False)
            )
        return created

    # ------------------------------------------------------------------ deliver

    async def _claim(self, org_id: uuid.UUID) -> list[_Claimed]:
        lease = timedelta(seconds=self._settings.webhook_timeout_seconds * 3 + 30)
        async with self._session(org_id) as session, session.begin():
            now = await _db_now(session)
            rows = list(
                (
                    await session.execute(
                        select(WebhookDelivery, WebhookEndpoint)
                        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
                        .where(
                            WebhookDelivery.status == DeliveryStatus.PENDING.value,
                            WebhookDelivery.next_attempt_at <= func.now(),
                            or_(
                                WebhookDelivery.locked_until.is_(None),
                                WebhookDelivery.locked_until < func.now(),
                            ),
                            WebhookEndpoint.active,
                        )
                        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
                        .limit(self._settings.webhook_batch_size)
                        .with_for_update(of=WebhookDelivery, skip_locked=True)
                    )
                ).all()
            )
            claimed: list[_Claimed] = []
            for delivery, endpoint in rows:
                delivery.attempts += 1
                delivery.locked_until = now + lease
                delivery.last_attempt_at = now
                claimed.append(
                    _Claimed(
                        delivery_id=delivery.id,
                        endpoint_id=endpoint.id,
                        event_id=delivery.event_id,
                        event_type=delivery.event_type,
                        attempt=delivery.attempts,
                        created_at=delivery.created_at,
                        url=endpoint.url,
                        secret=str(unseal(endpoint, "secret")),
                        body=serialise(delivery.payload),
                    )
                )
            await session.flush()
        return claimed

    def _request(self, claimed: _Claimed) -> OutgoingWebhook:
        return OutgoingWebhook(
            delivery_id=claimed.delivery_id,
            url=claimed.url,
            body=claimed.body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                SIGNATURE_HEADER: sign_payload(claimed.secret, claimed.body),
                "Pairly-Event-Id": event_ref(claimed.event_id),
                "Pairly-Event-Type": claimed.event_type,
                "Pairly-Delivery-Id": str(claimed.delivery_id),
                "Pairly-Delivery-Attempt": str(claimed.attempt),
            },
        )

    async def deliver_due(
        self, org_id: uuid.UUID, stats: DeliveryStats | None = None
    ) -> DeliveryStats:
        stats = stats or DeliveryStats()
        claimed = await self._claim(org_id)
        if not claimed:
            return stats
        semaphore = asyncio.Semaphore(self._settings.webhook_concurrency)

        async def one(item: _Claimed) -> tuple[_Claimed, SendResult]:
            async with semaphore:
                try:
                    return item, await self._sender.send(self._request(item))
                except Exception as exc:  # a sender bug counts as a failed attempt
                    return item, SendResult(None, 0, type(exc).__name__[:64])

        results = await asyncio.gather(*(one(item) for item in claimed))
        for item, result in results:
            stats.sent += 1
            await self._record(org_id, item, result, stats)
        return stats

    async def _record(
        self, org_id: uuid.UUID, item: _Claimed, result: SendResult, stats: DeliveryStats
    ) -> None:
        async with self._session(org_id) as session, session.begin():
            now = await _db_now(session)
            values: dict[str, Any] = {
                "response_code": result.status_code,
                "latency_ms": result.latency_ms,
                "last_error": result.error or (None if result.ok else f"http_{result.status_code}"),
                "locked_until": None,
            }
            if result.ok:
                stats.succeeded += 1
                values |= {"status": DeliveryStatus.SUCCEEDED.value, "completed_at": now}
                await session.execute(
                    update(WebhookEndpoint)
                    .where(WebhookEndpoint.id == item.endpoint_id)
                    .values(consecutive_failures=0)
                    .execution_options(synchronize_session=False)
                )
            else:
                stats.failed_attempts += 1
                delay = retry_delay(item.attempt, rng=self._rng)
                next_at = now + timedelta(seconds=delay) if delay is not None else None
                if next_at is None or next_at > item.created_at + MAX_AGE:
                    stats.gave_up += 1
                    values |= {"status": DeliveryStatus.FAILED.value, "completed_at": now}
                else:
                    values |= {"next_attempt_at": next_at}
            await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == item.delivery_id,
                    WebhookDelivery.status == DeliveryStatus.PENDING.value,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if not result.ok:
                await self._count_failure(session, item, result, stats)

    async def _count_failure(
        self, session: AsyncSession, item: _Claimed, result: SendResult, stats: DeliveryStats
    ) -> None:
        row = (
            await session.execute(
                update(WebhookEndpoint)
                .where(WebhookEndpoint.id == item.endpoint_id, WebhookEndpoint.active)
                .values(consecutive_failures=WebhookEndpoint.consecutive_failures + 1)
                .returning(WebhookEndpoint.consecutive_failures)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if row is None or int(row[0]) < self._settings.webhook_max_consecutive_failures:
            return
        endpoint = await session.scalar(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.id == item.endpoint_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if endpoint is None or not endpoint.active:
            return
        now = await _db_now(session)
        endpoint.active = False
        endpoint.disabled_at = now
        endpoint.disabled_reason = "consecutive_failures"
        endpoint.updated_at = now
        cancelled: Any = await session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.status == DeliveryStatus.PENDING.value,
            )
            .values(status=DeliveryStatus.CANCELLED.value, completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await session.flush()
        await append_audit(
            session,
            action=DISABLED_ACTION,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR,
            entity_type="webhook_endpoint",
            entity_id=endpoint.id,
            payload={
                "reason": "consecutive_failures",
                "consecutive_failures": int(row[0]),
                "last_error": result.error or f"http_{result.status_code}",
                "cancelled_deliveries": int(cancelled.rowcount or 0),
            },
        )
        await self._on_disabled(session, endpoint)
        stats.disabled_endpoints.append(str(endpoint.id))
        log.warning("webhook_endpoint_disabled", endpoint_id=str(endpoint.id), failures=int(row[0]))

    # ------------------------------------------------------------------ all orgs

    async def enabled(self, org_id: uuid.UUID) -> bool:
        """Whether the organisation delivers webhooks now (the ``enabled_for`` check)."""
        if self._enabled_for is None:
            return True
        async with self._session(org_id) as session, session.begin():
            return await self._enabled_for(session)

    async def run_org(self, org_id: uuid.UUID) -> DeliveryStats:
        stats = DeliveryStats()
        # Switched off: touch nothing. The outbox keeps its events and pending deliveries
        # stay pending, so switching back on delivers them (within the 24 hour window).
        if not await self.enabled(org_id):
            stats.skipped = True
            return stats
        stats.fanned_out = await self.fan_out(org_id)
        return await self.deliver_due(org_id, stats)


async def due_org_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Organisations with undispatched events or due deliveries (SECURITY DEFINER)."""
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM webhook_due_org_ids() AS org_id"))
        return [uuid.UUID(str(v)) for v in rows.scalars()]

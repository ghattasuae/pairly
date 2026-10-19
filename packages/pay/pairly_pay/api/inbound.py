# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``POST /v1/pay/inbound/resend``: Resend's ``email.received`` webhook for Pay's intake
address (``pairly_pay.resend_inbound``).

No bearer token: every request must carry a valid Resend (Svix) signature over the raw
body, at most five minutes old, or it gets 401 before anything is parsed. Every request,
signed or not, first counts against a per-client-address limit (``LIMIT`` a minute),
shared between API processes through Valkey like the other unauthenticated routes
(``pairly_core.ratelimit``, with this process's window as the fallback); over it, 429.
Resend retries a 429 later. A rejected signature is logged with its reason only, never
the body or the headers. Without
``PAIRLY_RESEND_WEBHOOK_SECRET`` and ``PAIRLY_RESEND_INBOUND_API_KEY`` it answers 503.
Not in the public OpenAPI document: it is a provider callback, not part of the API.

Answers: 200 for processed, duplicate, ignored (other event types) and dropped
(unroutable, too large, a message Resend no longer has) events; 503 when Resend or
storage is temporarily unavailable and 500 on any other failure, so Resend retries.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pairly_api.routes.notifications import NotifySettings
from pairly_api.uploads import get_services
from pairly_core.errors import ServiceUnavailableError, UnauthenticatedError
from pairly_core.identity import Context
from pairly_core.integrations.resend.receiving import ReceivingError, ResendReceivingClient
from pairly_core.integrations.resend.signature import (
    MAX_BODY_BYTES,
    ResendSignatureError,
    verify,
)
from pairly_core.ratelimit import window_limiters
from pairly_pay.api.onboarding import _Window, client_ip
from pairly_pay.resend_inbound import handle_received, parse_event

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/inbound", tags=["pay"], include_in_schema=False)

RECEIVING_STATE: Final = "resend_receiving_client"
LIMITER_STATE: Final = "pay_resend_inbound_limiters"
# Requests a minute per client address. Behind a proxy that does not pass the client
# address (PAIRLY_TRUSTED_PROXIES), this is one limit for every caller together.
LIMITS: Final = {"webhook": (300, 60.0)}


async def _limit(request: Request) -> None:
    store = window_limiters(request, LIMITER_STATE, LIMITS, "pay.resend_inbound", local=_Window)
    await store["webhook"].hit(client_ip(request))


async def _limited_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        raise UnauthenticatedError("invalid Resend request")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise UnauthenticatedError("invalid Resend request")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/resend")
async def resend_received(request: Request, ctx: Context, settings: NotifySettings) -> JSONResponse:
    secret = settings.resend_webhook_secret
    key = settings.resend_inbound_api_key
    if secret is None or key is None:
        raise ServiceUnavailableError("Resend inbound email is not configured")
    # Before anything is read or verified, so unsigned floods count too.
    await _limit(request)
    body = await _limited_body(request)
    try:
        msg_id = verify(
            secret=secret.get_secret_value().strip(),
            msg_id=request.headers.get("svix-id"),
            timestamp=request.headers.get("svix-timestamp"),
            signature=request.headers.get("svix-signature"),
            body=body,
        )
    except ResendSignatureError as exc:
        log.warning("resend_signature_rejected", reason=exc.reason)
        raise UnauthenticatedError("invalid Resend request") from None
    event = parse_event(msg_id, body)
    if event is None:
        return JSONResponse({"ok": True, "outcome": "ignored"})
    services = get_services(request)
    client = getattr(request.app.state, RECEIVING_STATE, None)
    owned = client is None
    if client is None:
        client = ResendReceivingClient(
            api_key=key.get_secret_value().strip(), base_url=settings.resend_base_url
        )
    try:
        outcome = await handle_received(
            event,
            sessionmaker=ctx.sessionmaker,
            store=services.store,
            ingestion=services.settings,
            notifications=settings,
            fetch_raw=lambda email_id, limit: client.raw_message(email_id, max_bytes=limit),
        )
    except ReceivingError as exc:
        log.warning("resend_receiving_failed", code=exc.code, transient=exc.transient)
        if exc.transient:
            raise ServiceUnavailableError("Resend is temporarily unavailable") from None
        return JSONResponse({"ok": True, "outcome": "dropped"})
    finally:
        if owned:
            await client.aclose()
    return JSONResponse({"ok": True, "outcome": outcome})

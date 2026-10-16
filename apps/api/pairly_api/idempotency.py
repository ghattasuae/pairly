# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``Idempotency-Key`` for POST endpoints that API clients call (WP1.11).

Two parts (storage and protocol: ``pairly_core.idempotency``):

* ``IdempotencyMiddleware`` (ASGI, inside the edge middleware). For a POST with an
  ``Idempotency-Key`` header it hashes the request as it streams in (method, path,
  query, content type without the multipart boundary, and the body with the multipart
  boundary normalised, so a client retrying with a fresh boundary still matches), and
  keeps the response (up to 1 MiB) to store it after it was sent.
* ``Idempotent``, a route dependency. Endpoints opt in with
  ``dependencies=[Depends(idempotent, scope="function")]``. It claims the key for the
  actor before the handler runs, stores the request hash just before the commit, and
  answers duplicates: replay (with ``Idempotent-Replayed: true``), 409 on a different
  request with the same key, 409 while the first request is still running.

Keys are per actor (user, agent or API key) and per organisation, kept for 24 hours.
Without the header nothing changes. Endpoints that return secrets (API key and webhook
endpoint creation) do not opt in, so no secret is ever stored for replay.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from fastapi import Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pairly_core.audit import actor_ref
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError, PairlyError
from pairly_core.idempotency import (
    KEY_RE,
    MAX_STORED_BODY,
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    StoredResponse,
    claim,
    load,
    set_request_hash,
    store_response,
    stored_response,
)
from pairly_core.identity import CurrentActor, OrgSession

log = structlog.get_logger("pairly_api.idempotency")

HEADER: Final = "idempotency-key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"
_HASHER: Final = "pairly.idempotency.hasher"
_TICKET: Final = "pairly.idempotency.ticket"
# Bodies a duplicate reads only to hash them; larger is refused.
MAX_HASHED_BODY: Final = 128 * 1024 * 1024
WAIT_STEPS: Final = 10
WAIT_SECONDS: Final = 0.2
_BOUNDARY_TOKEN: Final = b"--pairly-boundary"


class PayloadTooLargeError(PairlyError):
    status = 413
    code = "payload_too_large"
    title = "Payload too large"


class IdempotentReplay(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Raised by the dependency to answer a duplicate with the stored response."""

    def __init__(self, response: StoredResponse) -> None:
        super().__init__("idempotent replay")
        self.response = response


async def replay_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, IdempotentReplay):
        raise exc
    stored = exc.response
    headers = dict(stored.headers)
    headers[REPLAYED_HEADER] = "true"
    return Response(content=stored.body, status_code=stored.status, headers=headers)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


def _boundary(content_type: str) -> bytes | None:
    main, _, params = content_type.partition(";")
    if main.strip().lower() != "multipart/form-data":
        return None
    for part in params.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "boundary" and value.strip():
            return value.strip().strip('"').encode("latin-1")
    return None


class BodyHasher:
    """SHA-256 of the request as it streams through ``receive``."""

    def __init__(self, scope: Scope, receive: Receive) -> None:
        self._receive = receive
        self._digest = hashlib.sha256()
        self._done = False
        self._seen = 0
        content_type = _header(scope, b"content-type") or ""
        boundary = _boundary(content_type)
        self._marker = b"--" + boundary if boundary else None
        self._tail = b""
        main_type = content_type.partition(";")[0].strip().lower()
        for part in (
            str(scope.get("method", "")).encode(),
            str(scope.get("path", "")).encode("utf-8", "surrogateescape"),
            bytes(scope.get("query_string", b"")),
            main_type.encode("latin-1"),
        ):
            self._digest.update(len(part).to_bytes(8, "big") + part)

    def _feed(self, data: bytes) -> None:
        self._seen += len(data)
        if self._marker is None:
            self._digest.update(data)
            return
        # Replace every boundary marker with a fixed token, across chunk edges.
        buf = self._tail + data
        out = bytearray()
        start = 0
        while (found := buf.find(self._marker, start)) >= 0:
            out += buf[start:found] + _BOUNDARY_TOKEN
            start = found + len(self._marker)
        rest = buf[start:]
        keep = len(self._marker) - 1
        if len(rest) > keep:
            out += rest[: len(rest) - keep]
            rest = rest[len(rest) - keep :]
        self._tail = rest
        self._digest.update(bytes(out))

    async def receive(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request" and not self._done:
            self._feed(message.get("body", b""))
            if not message.get("more_body", False):
                self._done = True
        elif message["type"] == "http.disconnect":
            self._done = True
        return message

    async def hexdigest(self) -> str:
        """The request hash, reading (and discarding) whatever body is still unread."""
        while not self._done:
            if self._seen > MAX_HASHED_BODY:
                raise PayloadTooLargeError("the request body is too large")
            await self.receive()
        digest = self._digest.copy()
        digest.update(self._tail)
        return digest.hexdigest()


@dataclass
class _Ticket:
    org_id: Any
    record_id: Any


@dataclass
class _Captured:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)
    truncated: bool = False


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _header(scope, HEADER.encode()) is None
        ):
            await self.app(scope, receive, send)
            return
        hasher = BodyHasher(scope, receive)
        state = scope.setdefault("state", {})
        state[_HASHER] = hasher
        captured = _Captured()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status = int(message["status"])
                captured.headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                }
            elif message["type"] == "http.response.body" and not captured.truncated:
                captured.body += message.get("body", b"")
                if len(captured.body) > MAX_STORED_BODY:
                    captured.truncated = True
                    captured.body = bytearray()
            await send(message)

        try:
            await self.app(scope, hasher.receive, send_wrapper)
        finally:
            ticket = state.pop(_TICKET, None)
            if isinstance(ticket, _Ticket) and captured.status:
                await self._store(scope, ticket, captured)

    @staticmethod
    async def _store(scope: Scope, ticket: _Ticket, captured: _Captured) -> None:
        app = scope.get("app")
        ctx = getattr(getattr(app, "state", None), "pairly", None)
        sessionmaker = getattr(ctx, "sessionmaker", None)
        if sessionmaker is None:  # pragma: no cover - the API always has a context
            return
        try:
            async with sessionmaker() as session:
                set_tenant(session, ticket.org_id, None)
                async with session.begin():
                    await store_response(
                        session,
                        ticket.record_id,
                        status=captured.status,
                        headers=captured.headers,
                        body=None if captured.truncated else bytes(captured.body),
                    )
        except Exception as exc:
            # The key then answers 409 until it expires; it never runs twice.
            log.error("idempotency_store_failed", error=type(exc).__name__)


async def idempotent(
    request: Request, actor: CurrentActor, session: OrgSession
) -> AsyncIterator[None]:
    """Route dependency: honour ``Idempotency-Key`` on this POST endpoint."""
    raw = request.headers.get(HEADER)
    if raw is None or request.method != "POST":
        yield
        return
    if not KEY_RE.fullmatch(raw):
        raise BadRequestError("Idempotency-Key must be 8 to 128 letters, digits or ._:-")
    hasher = request.scope.get("state", {}).get(_HASHER)
    if not isinstance(hasher, BodyHasher):
        raise BadRequestError("this endpoint cannot honour Idempotency-Key here")
    actor_type, actor_id = actor_ref(actor)
    claimed = await claim(session, actor_type=actor_type.value, actor_id=actor_id, key=raw)
    if not claimed.fresh:
        digest = await hasher.hexdigest()
        for _ in range(WAIT_STEPS):
            record = await load(session, claimed.record_id)
            if record is None:  # pragma: no cover - expired and purged meanwhile
                break
            if record.request_hash is not None and record.request_hash != digest:
                raise IdempotencyKeyReusedError()
            response = stored_response(record)
            if response is not None:
                raise IdempotentReplay(response)
            await session.rollback()  # end the snapshot, then look again
            await asyncio.sleep(WAIT_SECONDS)
        raise IdempotencyInProgressError()
    request.scope.setdefault("state", {})[_TICKET] = _Ticket(actor.org_id, claimed.record_id)
    yield
    # Success path only (an exception rolls the claim back with the request).
    await set_request_hash(session, claimed.record_id, await hasher.hexdigest())

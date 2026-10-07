# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Edge middleware: request ids, access log, security headers, last-resort 500 handler.

It is a plain ASGI middleware placed outermost, so every response (including errors and
CORS preflights) carries the request id and the security headers.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pairly_api.problems import PROBLEM_JSON, problem_body

log = structlog.get_logger("pairly_api.access")

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")

_CSP_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Swagger UI (non-production only) loads its assets from jsdelivr.
_CSP_DOCS = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect"})
# Paths that carry a secret token; the token segment is never logged.
_TOKEN_PATH_PREFIXES = ("/v1/approval-links/",)


def loggable_path(path: str) -> str:
    """``path`` with secret token segments replaced by ``[redacted]``."""
    for prefix in _TOKEN_PATH_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            return prefix + "[redacted]"
    return path


class EdgeMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    def _security_headers(self, path: str) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"content-security-policy", (_CSP_DOCS if path in _DOCS_PATHS else _CSP_API).encode()),
        ]
        if self.hsts:
            headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = (
            dict(scope.get("headers") or []).get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        )
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_holder: dict[str, int] = {}
        extra = self._security_headers(path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {h for h, _ in extra}
                ]
                headers.extend(extra)
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled_error", method=scope.get("method"), path=loggable_path(path))
            if "status" in status_holder:
                raise
            body = json.dumps(
                problem_body(500, "internal_error", title="Internal error", request_id=request_id)
            ).encode()
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", PROBLEM_JSON.encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            self._access_log(scope, status_holder.get("status", 500), started)

    @staticmethod
    def _access_log(scope: Scope, status: int, started: float) -> None:
        state: dict[str, Any] = scope.get("state", {})
        actor = state.get("actor")
        fields: dict[str, Any] = {
            "method": scope.get("method"),
            "path": loggable_path(str(scope.get("path", ""))),
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if actor is not None:
            fields["user_id"] = str(actor.id)
            fields["org_id"] = str(actor.org_id)
        log.info("request", **fields)

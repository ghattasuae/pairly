# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The MCP server's only way to the Pairly API: a small client with a route allowlist.

Every tool call goes through the API, so authentication, Row Level Security, RBAC and
the API key limits (keys only read, propose and upload) all apply on the server. This
client adds a second, local fence: it can only send the requests in ``ALLOWED_ROUTES``,
which are reads plus ``POST /v1/proposals`` and two searches. No decision, approval,
payment, export or delete route is on the list, so no tool can reach one whatever the
model asks for. Path segments are validated (UUIDs, decision ids) before a URL is
built, so an argument cannot walk to another route either.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Final

import httpx

from pairly_mcp import __version__
from pairly_mcp.config import McpConfig

ORG_HEADER: Final = "X-Pairly-Org"
DECISION_ID_RE: Final = re.compile(r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")
_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# (method, path pattern). Reads, two searches (POST bodies, no side effect) and the
# proposal route. Nothing else, ever: see tests/test_mcp_routes.py.
ALLOWED_ROUTES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, re.compile(f"^{pattern}$"))
    for method, pattern in (
        ("GET", "/v1/me"),
        ("POST", "/v1/hire/rediscovery/search"),
        ("GET", "/v1/hire/applications"),
        ("GET", f"/v1/hire/pipeline/applications/{_UUID}"),
        ("GET", "/v1/approvals"),
        ("GET", f"/v1/approvals/{_UUID}"),
        ("GET", "/v1/pay/invoices"),
        ("GET", f"/v1/pay/invoices/{_UUID}"),
        ("GET", "/v1/pay/exceptions"),
        ("POST", "/v1/proposals"),
        ("GET", "/v1/decisions/dec_[0-9A-HJKMNP-TV-Z]{26}/receipt"),
    )
)


class RouteNotAllowedError(RuntimeError):
    """A request outside ``ALLOWED_ROUTES`` (a programming error, never sent)."""


class ApiError(Exception):
    """The API answered with an error (RFC 9457 problem details)."""

    def __init__(self, status: int, title: str, detail: str, code: str | None) -> None:
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code


def route_allowed(method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in ALLOWED_ROUTES)


def uuid_segment(value: str, what: str) -> str:
    """``value`` as a canonical UUID path segment, or ``ValueError``."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except ValueError:
        raise ValueError(f"{what} must be an id (a UUID)") from None


def decision_segment(value: str) -> str:
    text = str(value).strip()
    if not DECISION_ID_RE.fullmatch(text):
        raise ValueError("decision_id must look like dec_ followed by 26 characters")
    return text


class PairlyApi:
    """Async client for the allowed routes. Close it with ``aclose``."""

    def __init__(
        self, config: McpConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        headers = {
            "Authorization": f"Bearer {config.credential}",
            "Accept": "application/json",
            "User-Agent": f"pairly-mcp/{__version__}",
        }
        if config.org_id is not None:
            headers[ORG_HEADER] = str(config.org_id)
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_url,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        if not route_allowed(method, path):
            raise RouteNotAllowedError(f"{method} {path} is not a route the MCP server may call")
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = await self._client.request(method, path, params=clean or None, json=json)
        if response.status_code >= 400:
            raise _api_error(response)
        if not response.content:
            return None
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any) -> Any:
        return await self.request("POST", path, json=body)


def _api_error(response: httpx.Response) -> ApiError:
    title, detail, code = response.reason_phrase or "Error", "", None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        title = str(body.get("title") or title)[:200]
        detail = str(body.get("detail") or "")[:500]
        raw_code = body.get("code")
        code = str(raw_code)[:64] if raw_code is not None else None
    return ApiError(response.status_code, title, detail, code)

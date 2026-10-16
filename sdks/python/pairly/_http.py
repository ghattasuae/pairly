# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Shared request logic for the sync and async clients.

* Credentials: an API key (``pk_live_...``/``pk_test_...``) or a person's JWT plus the
  organisation id (``X-Pairly-Org``).
* Retries: 429 (honouring ``Retry-After``), 500, 502, 503, 504 and connection errors,
  with exponential backoff and full jitter, at most ``max_retries`` times. Every POST
  carries an ``Idempotency-Key`` (one per call, the same on each retry), so a retried
  POST is never applied twice by the endpoints that honour it.
* Errors: problem details become ``PairlyAPIError`` subclasses.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from pairly._version import __version__
from pairly.errors import PairlyAPIError, PairlyConfigError, error_for

DEFAULT_BASE_URL: Final = "http://127.0.0.1:8000"
ORG_HEADER: Final = "X-Pairly-Org"
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER: Final = 60.0
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: Callable[[], float] = field(default=random.random, repr=False)

    def delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based)."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), MAX_RETRY_AFTER)
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return float(ceiling * self.jitter())


@dataclass(frozen=True, slots=True)
class ClientConfig:
    base_url: str
    credential: str = field(repr=False)
    org_id: str | None
    timeout: float
    retry: RetryPolicy
    user_agent: str

    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.credential}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.org_id:
            headers[ORG_HEADER] = self.org_id
        return headers


def make_config(
    *,
    api_key: str | None,
    token: str | None,
    org_id: str | uuid.UUID | None,
    base_url: str | None,
    timeout: float,
    max_retries: int,
    retry: RetryPolicy | None = None,
    allow_http: bool = False,
) -> ClientConfig:
    """Settings from the arguments, else ``PAIRLY_API_KEY``, ``PAIRLY_TOKEN``,
    ``PAIRLY_ORG`` and ``PAIRLY_API_URL``."""
    if api_key is None and token is None:
        api_key = os.environ.get("PAIRLY_API_KEY") or None
        token = None if api_key else os.environ.get("PAIRLY_TOKEN") or None
    if (api_key is None) == (token is None):
        raise PairlyConfigError("pass exactly one of api_key and token (or set PAIRLY_API_KEY)")
    if api_key is not None and not api_key.startswith(("pk_live_", "pk_test_")):
        raise PairlyConfigError("api_key must be a Pairly API key (pk_live_... or pk_test_...)")
    org = str(org_id) if org_id is not None else os.environ.get("PAIRLY_ORG") or None
    if org is not None:
        try:
            org = str(uuid.UUID(org))
        except ValueError:
            raise PairlyConfigError("org_id must be an organisation id (a UUID)") from None
    if token is not None and org is None:
        raise PairlyConfigError("a token needs org_id (or PAIRLY_ORG)")
    url = (base_url or os.environ.get("PAIRLY_API_URL") or DEFAULT_BASE_URL).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PairlyConfigError("base_url must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK and not allow_http:
        raise PairlyConfigError(
            "base_url must use https unless it points at this machine (or allow_http=True)"
        )
    if max_retries < 0:
        raise PairlyConfigError("max_retries must be 0 or more")
    policy = retry or RetryPolicy(max_retries=max_retries)
    return ClientConfig(
        base_url=url,
        credential=str(api_key or token),
        org_id=org,
        timeout=timeout,
        retry=policy,
        user_agent=f"pairly-python/{__version__}",
    )


@dataclass(frozen=True, slots=True)
class Call:
    """One API call, built by a resource method and run by either client."""

    method: str
    path: str
    params: Mapping[str, Any] | None = None
    json: Any = None
    files: Any = None
    data: Mapping[str, str] | None = None
    idempotency_key: str | None = None

    def request_kwargs(self) -> dict[str, Any]:
        headers: dict[str, str] = {}
        key = self.idempotency_key
        if self.method == "POST":
            headers["Idempotency-Key"] = key or f"py-{uuid.uuid4().hex}"
        clean = {k: _param(v) for k, v in (self.params or {}).items() if v is not None}
        kwargs: dict[str, Any] = {"headers": headers}
        if clean:
            kwargs["params"] = clean
        if self.json is not None:
            kwargs["json"] = self.json
        if self.files is not None:
            kwargs["files"] = self.files
        if self.data is not None:
            kwargs["data"] = dict(self.data)
        return kwargs


def _param(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    from datetime import UTC, datetime

    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def should_retry(response: httpx.Response) -> bool:
    return response.status_code in RETRY_STATUSES


def raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    title, detail, code, request_id = response.reason_phrase or "Error", "", None, None
    if isinstance(body, dict):
        title = str(body.get("title") or title)
        detail = str(body.get("detail") or "")
        code = str(body["code"]) if body.get("code") is not None else None
        request_id = str(body["request_id"]) if body.get("request_id") is not None else None
    request_id = request_id or response.headers.get("x-request-id")
    error: type[PairlyAPIError] = error_for(response.status_code)
    raise error(
        response.status_code,
        title=title,
        detail=detail,
        code=code,
        request_id=request_id,
        retry_after=retry_after_seconds(response),
        body=body,
    )


def decode(response: httpx.Response) -> Any:
    raise_for(response)
    if not response.content:
        return None
    return response.json()

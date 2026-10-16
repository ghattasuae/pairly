# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bot and spam protection for the public apply flow (brief 3.2.3).

* ``TurnstileVerifier``: checks a Cloudflare Turnstile token. ``FakeTurnstileVerifier``
  (development and test) accepts only Cloudflare's documented dummy token
  ``XXXX.DUMMY.TOKEN.XXXX``, so nothing reaches Cloudflare. ``CloudflareTurnstileVerifier``
  calls the siteverify endpoint with the secret (``PAIRLY_HIRE_TURNSTILE_SECRET``); it is
  not configured yet. Outside development and test the fake is refused: without a real
  verifier the public apply routes answer 503 (fail closed).
* ``is_disposable(email)``: the bundled disposable domain list
  (``data/disposable_domains.txt``, CC0), subdomains included.
* ``SlidingWindowLimiter``: per IP request limits in process memory, bounded. The
  routes use it through ``limiters`` (``pairly_core.ratelimit.SharedWindowLimiter``):
  every request also draws from a token bucket in Valkey shared by every API process,
  and is refused when either refuses; with Valkey down the local window alone decides
  (security review 2026-10, L-03). Per email limits are counted in the database
  (``pairly_hire.applying``), so they hold across processes.
* ``client_ip(request)``: the socket peer, or the ``X-Pairly-Client-IP`` header when
  the peer is a trusted proxy (the web app's server, which sets it from the browser's
  connection). Trusted proxies: ``PAIRLY_HIRE_TRUSTED_PROXIES`` (comma separated IPs),
  default loopback only.
"""

from __future__ import annotations

import ipaddress
import math
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from functools import cache
from importlib.resources import files
from typing import Final, Protocol, runtime_checkable

import httpx
import structlog
from fastapi import Request

from pairly_core.config import Environment
from pairly_core.errors import PairlyError, ServiceUnavailableError
from pairly_core.ratelimit import SharedWindowLimiter, window_limiters

log = structlog.get_logger("pairly_hire.antispam")

DUMMY_TOKEN: Final = "XXXX.DUMMY.TOKEN.XXXX"  # noqa: S105  (Cloudflare public test value)
SITEVERIFY_URL: Final = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
CLIENT_IP_HEADER: Final = "x-pairly-client-ip"
TURNSTILE_ENV: Final = "PAIRLY_HIRE_TURNSTILE"
TURNSTILE_SECRET_ENV: Final = "PAIRLY_HIRE_TURNSTILE_SECRET"  # noqa: S105  (a variable name)
TRUSTED_PROXIES_ENV: Final = "PAIRLY_HIRE_TRUSTED_PROXIES"
MAX_TOKEN_LENGTH: Final = 2048


class RateLimitedError(PairlyError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(
        self, retry_after: int, detail: str = "too many requests, try again later"
    ) -> None:
        super().__init__(detail)
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}


# ------------------------------------------------------------------ Turnstile


@runtime_checkable
class TurnstileVerifier(Protocol):
    name: str

    async def verify(self, token: str, remote_ip: str | None) -> bool: ...


class FakeTurnstileVerifier:
    """Development and test only: accepts Cloudflare's dummy token, nothing else."""

    name = "fake"

    async def verify(self, token: str, remote_ip: str | None) -> bool:
        del remote_ip
        return token == DUMMY_TOKEN


class CloudflareTurnstileVerifier:
    """The real check (``siteverify``). Needs the site's secret key."""

    name = "cloudflare"

    def __init__(self, secret: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not secret:
            raise ValueError("a Turnstile secret is required")
        self._secret = secret
        self._client = client

    async def verify(self, token: str, remote_ip: str | None) -> bool:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        data = {"secret": self._secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            if self._client is not None:
                response = await self._client.post(SITEVERIFY_URL, data=data, timeout=5.0)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.post(SITEVERIFY_URL, data=data)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Fail closed: an unverifiable token is a failed check.
            log.warning("turnstile_unavailable", error=type(exc).__name__)
            return False
        return isinstance(payload, dict) and payload.get("success") is True


def turnstile_from_env(env: Environment) -> TurnstileVerifier:
    """The verifier this process uses. Raises ``ServiceUnavailableError`` (fail closed)
    when production has no real verifier configured."""
    mode = os.environ.get(TURNSTILE_ENV, "").strip().lower()
    secret = os.environ.get(TURNSTILE_SECRET_ENV, "").strip()
    if mode == "cloudflare" or (not mode and secret):
        if not secret:
            raise ServiceUnavailableError("applications are closed: bot protection is not set up")
        return CloudflareTurnstileVerifier(secret)
    if env in {Environment.DEVELOPMENT, Environment.TEST}:
        return FakeTurnstileVerifier()
    raise ServiceUnavailableError("applications are closed: bot protection is not set up")


def verifier_for(request: Request, env: Environment) -> TurnstileVerifier:
    """A verifier on ``app.state.hire_turnstile`` (tests) wins over the environment."""
    override = getattr(request.app.state, "hire_turnstile", None)
    if isinstance(override, TurnstileVerifier):
        return override
    return turnstile_from_env(env)


# ------------------------------------------------------------------ disposable email


@cache
def disposable_domains() -> frozenset[str]:
    text = files("pairly_hire").joinpath("data", "disposable_domains.txt").read_text("utf-8")
    return frozenset(
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def is_disposable(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if not domain:
        return False
    listed = disposable_domains()
    parts = domain.split(".")
    return any(".".join(parts[i:]) in listed for i in range(len(parts) - 1))


# ------------------------------------------------------------------ rate limits


class SlidingWindowLimiter:
    """At most ``limit`` hits per key in any ``window`` seconds. Bounded memory."""

    def __init__(
        self,
        limit: int,
        window: float,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys
        self._clock = clock

    def hit(self, key: str) -> None:
        """Count one hit for ``key`` or raise ``RateLimitedError``."""
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
            if len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            retry = max(1, math.ceil(hits[0] + self.window - now))
            raise RateLimitedError(retry)
        hits.append(now)


def limiters(
    request: Request, attr: str, limits: Mapping[str, tuple[int, float]], namespace: str
) -> dict[str, SharedWindowLimiter]:
    """The shared limiters of one set of Hire routes (``await limiter.hit(key)``)."""
    return window_limiters(
        request, attr, limits, namespace, local=SlidingWindowLimiter, error=RateLimitedError
    )


# ------------------------------------------------------------------ client address


@cache
def _trusted_proxies() -> frozenset[str]:
    raw = os.environ.get(TRUSTED_PROXIES_ENV, "127.0.0.1,::1")
    trusted: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            trusted.add(str(ipaddress.ip_address(item)))
        except ValueError:
            log.warning("trusted_proxy_ignored", reason="not an IP address")
    return frozenset(trusted)


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    peer = _valid_ip(request.client.host if request.client else None)
    if peer is not None and peer in _trusted_proxies():
        forwarded = _valid_ip(request.headers.get(CLIENT_IP_HEADER))
        if forwarded is not None:
            return forwarded
    return peer or "unknown"

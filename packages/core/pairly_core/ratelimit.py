# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Request rate limits shared by every API process: a token bucket per key in Valkey.

With one limiter per process, N API processes allowed N times each limit (security
review 2026-10, L-03). The shared limit lives in Valkey (arq's Redis connection, the one
the job queue uses): ``SharedBuckets`` runs an atomic Lua token bucket per key, on the
server's clock, so every process draws from the same bucket.

The in-process limiters (``pairly_core.api_keys.ratelimit.RateLimiter`` for API keys
and batches, a sliding window for the portals, the public apply flow and magic links)
stay, as the fallback when Valkey is down or slow. Every request is counted in both,
and refused when either one refuses (the stricter of the two, so a request is never let
through only because Valkey did not answer). While Valkey is unreachable the local
limiter alone decides, and its bucket already holds the recent history, so knocking
Valkey over gives nobody a fresh burst. After a failure Valkey is left alone for
``cooldown`` seconds (a breaker), so a dead server costs one timeout, not one per
request.

Keys are hashed (SHA-256 under a namespace) before they reach Valkey: an email address
or an IP never sits there in the clear. A bucket expires once it would be full again.

* ``SharedRateLimiter``: ``await check(key, per_minute, cost)``, a token bucket that
  refills ``per_minute`` tokens a minute (API keys, batch items).
* ``SharedWindowLimiter``: ``await hit(key)``, at most ``limit`` hits in any ``window``
  seconds locally and a bucket of ``limit`` refilling ``limit / window`` a second in
  Valkey (portals, public forms, magic links).
* ``shared_buckets(request)``: the process's ``SharedBuckets`` (``app.state``), or one
  without Valkey (local limits only) when the app has none.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

import structlog

from pairly_core.api_keys.ratelimit import RateLimitedError, RateLimiter

log = structlog.get_logger("pairly_core.ratelimit")

PREFIX: Final = "pairly:rl:"
API_KEY_NAMESPACE: Final = "api_key"
BATCH_NAMESPACE: Final = "batch_items"
TIMEOUT_SECONDS: Final = 0.25
COOLDOWN_SECONDS: Final = 5.0

# KEYS[1] the bucket; ARGV: capacity, refill per second, cost. Returns {allowed, retry
# after in whole seconds}. The time is the server's (TIME), the same for every process.
_LUA: Final = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(state[1])
local at = tonumber(state[2])
if tokens == nil or at == nil then
  tokens = capacity
  at = now
end
tokens = math.min(capacity, tokens + math.max(0, now - at) * rate)
local allowed = 0
local retry = 0
if cost <= capacity and tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = math.ceil((math.min(cost, capacity) - tokens) / rate)
  if retry < 1 then retry = 1 end
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'at', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil((capacity - tokens) / rate * 1000) + 1000)
return {allowed, retry}
"""


class SharedUnavailableError(Exception):
    """Valkey did not answer (not configured, down, slow or erroring)."""


class _Script(Protocol):
    async def __call__(self, keys: list[str], args: list[Any]) -> Any: ...


class SharedBuckets:
    """Token buckets in Valkey. ``redis``: a ``redis.asyncio.Redis`` (arq's pool is
    one), or ``None`` to run on the local limiters only."""

    def __init__(
        self,
        redis: Any | None,
        *,
        prefix: str = PREFIX,
        timeout: float = TIMEOUT_SECONDS,
        cooldown: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._script: _Script | None = redis.register_script(_LUA) if redis is not None else None
        self._prefix = prefix
        self._timeout = timeout
        self._cooldown = cooldown
        self._clock = clock
        self._down_until = 0.0

    @property
    def configured(self) -> bool:
        return self._script is not None

    def bucket_key(self, namespace: str, key: str) -> str:
        digest = hashlib.sha256(f"{namespace}\x00{key}".encode()).hexdigest()[:40]
        return f"{self._prefix}{namespace}:{digest}"

    async def take(
        self, namespace: str, key: str, *, capacity: int, per_second: float, cost: int = 1
    ) -> int | None:
        """Take ``cost`` tokens: ``None`` when allowed, else the seconds to wait. Raises
        ``SharedUnavailableError`` when Valkey cannot answer now."""
        if self._script is None:
            raise SharedUnavailableError("no Valkey configured")
        if capacity < 1 or per_second <= 0 or cost < 1:
            return 60
        now = self._clock()
        if now < self._down_until:
            raise SharedUnavailableError("Valkey failed a moment ago")
        try:
            result = await asyncio.wait_for(
                self._script(
                    keys=[self.bucket_key(namespace, key)],
                    args=[capacity, repr(float(per_second)), cost],
                ),
                timeout=self._timeout,
            )
            allowed, retry = int(result[0]), int(result[1])
        except Exception as exc:  # any failure: the local limiter decides (fail closed)
            self._down_until = now + self._cooldown
            log.warning("rate_limit_shared_unavailable", error=type(exc).__name__)
            raise SharedUnavailableError(type(exc).__name__) from None
        return None if allowed == 1 else max(1, retry)


_NO_VALKEY: Final = SharedBuckets(None)


def shared_buckets(request: Any) -> SharedBuckets:
    """The process's shared buckets (``app.state.rate_buckets``), or local limits only."""
    buckets = getattr(getattr(getattr(request, "app", None), "state", None), "rate_buckets", None)
    return buckets if isinstance(buckets, SharedBuckets) else _NO_VALKEY


async def _shared(
    buckets: SharedBuckets | None,
    namespace: str,
    key: str,
    *,
    capacity: int,
    per_second: float,
    cost: int,
) -> int | None:
    if buckets is None:
        raise SharedUnavailableError("no shared buckets")
    return await buckets.take(namespace, key, capacity=capacity, per_second=per_second, cost=cost)


def _decide(local_retry: int | None, shared: int | None, error: RateLimitedError | None) -> None:
    """Refused when either limiter refused (the stricter of the two)."""
    waits = [w for w in (local_retry, shared) if w is not None]
    if waits:
        if error is not None and local_retry is not None and shared is None:
            raise error
        raise RateLimitedError(max(waits))


class SharedRateLimiter:
    """A token bucket per key that refills ``per_minute`` a minute, shared through
    Valkey, with the in-process ``RateLimiter`` as the fallback (see the module)."""

    def __init__(
        self,
        namespace: str,
        *,
        buckets: SharedBuckets | None = None,
        local: RateLimiter | None = None,
    ) -> None:
        self.namespace = namespace
        self.buckets = buckets
        self.local = local or RateLimiter()

    async def check(self, key: str, per_minute: int, cost: int = 1) -> None:
        """Take ``cost`` requests from ``key``'s buckets or raise ``RateLimitedError``."""
        local_error: RateLimitedError | None = None
        try:
            self.local.check(key, per_minute, cost)
        except RateLimitedError as exc:
            local_error = exc
        if per_minute < 1 or cost < 1 or cost > per_minute:
            raise local_error or RateLimitedError(60)
        try:
            shared = await _shared(
                self.buckets,
                self.namespace,
                key,
                capacity=per_minute,
                per_second=per_minute / 60.0,
                cost=cost,
            )
        except SharedUnavailableError:
            if local_error is not None:
                raise local_error from None
            return
        _decide(local_error.retry_after if local_error else None, shared, local_error)


class _LocalWindow(Protocol):
    limit: int
    window: float

    def hit(self, key: str) -> None: ...


class SharedWindowLimiter:
    """At most ``limit`` hits per key in any ``window`` seconds in this process, and a
    bucket of ``limit`` refilling ``limit / window`` a second shared through Valkey.
    ``local`` is the in-process limiter (each package's own sliding window); its
    ``RateLimitedError`` is raised as it is, so callers keep their status and wording."""

    def __init__(
        self,
        namespace: str,
        local: _LocalWindow,
        *,
        buckets: SharedBuckets | None = None,
        error: Callable[[int], Exception] | None = None,
    ) -> None:
        self.namespace = namespace
        self.local = local
        self.buckets = buckets
        self._error = error or RateLimitedError

    @property
    def limit(self) -> int:
        return self.local.limit

    @property
    def window(self) -> float:
        return self.local.window

    async def hit(self, key: str) -> None:
        """Count one hit for ``key`` or raise the local limiter's ``RateLimitedError``
        (or ``error(retry_after)`` when only the shared bucket refused)."""
        local_error: Exception | None = None
        try:
            self.local.hit(key)
        except Exception as exc:
            if getattr(exc, "status", None) != 429:
                raise
            local_error = exc
        try:
            shared = await _shared(
                self.buckets,
                self.namespace,
                key,
                capacity=self.local.limit,
                per_second=self.local.limit / self.local.window,
                cost=1,
            )
        except SharedUnavailableError:
            if local_error is not None:
                raise local_error from None
            return
        if shared is None:
            if local_error is not None:
                raise local_error
            return
        local_wait = int(getattr(local_error, "retry_after", 0) or 0)
        raise self._error(max(shared, local_wait, 1))


def window_limiters(
    request: Any,
    attr: str,
    limits: Mapping[str, tuple[int, float]],
    namespace: str,
    *,
    local: Callable[[int, float], _LocalWindow],
    error: Callable[[int], Exception] | None = None,
) -> dict[str, SharedWindowLimiter]:
    """One ``SharedWindowLimiter`` per kind of ``limits`` (``kind: (limit, window)``),
    kept on ``app.state.<attr>`` (so each app, and each test app, starts fresh), drawing
    from the process's shared buckets under ``<namespace>.<kind>``."""
    state = request.app.state
    store: dict[str, SharedWindowLimiter] | None = getattr(state, attr, None)
    if store is None:
        buckets = shared_buckets(request)
        store = {
            kind: SharedWindowLimiter(
                f"{namespace}.{kind}", local(limit, window), buckets=buckets, error=error
            )
            for kind, (limit, window) in limits.items()
        }
        setattr(state, attr, store)
    return store

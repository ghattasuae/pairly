# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Per-key request rate limit: a token bucket per API key, in process memory.

Each key may burst up to its per-minute limit and refills at ``limit / 60`` requests per
second. Over the limit the request gets 429 with ``Retry-After``.

Scope: one limiter per API process. With several API processes the effective limit is
``processes x limit`` (accepted for now). TODO(M6): a shared bucket in Valkey (an
atomic Lua token bucket per key prefix) when the API runs replicated.
The table is bounded (least recently used keys are dropped first), so many distinct keys
cannot grow memory without limit.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from pairly_core.errors import PairlyError


class RateLimitedError(PairlyError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, retry_after: int) -> None:
        super().__init__("the API key's request rate limit is exceeded")
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(
        self, *, max_keys: int = 100_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._max_keys = max_keys
        self._clock = clock

    def check(self, key: str, per_minute: int, cost: int = 1) -> None:
        """Take ``cost`` requests from ``key``'s bucket or raise ``RateLimitedError``."""
        if per_minute < 1 or cost < 1 or cost > per_minute:
            raise RateLimitedError(60)
        now = self._clock()
        rate = per_minute / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(per_minute), updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
            elapsed = max(now - bucket.updated, 0.0)
            bucket.tokens = min(float(per_minute), bucket.tokens + elapsed * rate)
            bucket.updated = now
        if bucket.tokens < cost:
            raise RateLimitedError(max(1, math.ceil((cost - bucket.tokens) / rate)))
        bucket.tokens -= cost

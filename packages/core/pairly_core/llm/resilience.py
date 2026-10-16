# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retries with exponential backoff and full jitter, and a per-provider circuit breaker.

Retry: only ``ProviderError.retryable`` errors (429, 408, 409, >= 500, timeouts,
connection errors) are retried. Delay before retry ``n`` (1-based) is
``uniform(0, min(max_delay, base_delay * 2**(n-1)))`` ("full jitter").

Breaker: closed until ``failure_threshold`` consecutive failures that say the provider
is unhealthy, then open (requests are not sent) for ``reset_seconds``, then half-open:
one trial request is let through; success closes it, failure opens it again.
The breaker is in-process state (one per worker process). That is enough to stop a
process hammering a provider that is down; it is not a cluster-wide quota.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pairly_core.llm.errors import ProviderError

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, retry_number: int, rng: random.Random | None = None) -> float:
        cap = min(self.max_delay, self.base_delay * (2 ** (retry_number - 1)))
        return (rng or _rng).uniform(0, cap)


_rng = random.SystemRandom()


async def call_with_retry[T](
    func: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Sleep = asyncio.sleep,
    on_failure: Callable[[ProviderError], None] | None = None,
    may_retry: Callable[[], bool] | None = None,
) -> tuple[T, int]:
    """Run ``func``; retry retryable ``ProviderError``s. Returns (result, retries used).

    ``on_failure`` sees every failed request (for the circuit breaker). ``may_retry`` is
    asked before each retry (the router stops when the breaker opened meanwhile).
    """
    retries = 0
    while True:
        try:
            return await func(), retries
        except ProviderError as exc:
            if on_failure is not None:
                on_failure(exc)
            if not exc.retryable or retries >= policy.max_retries:
                raise
            if may_retry is not None and not may_retry():
                raise
            retries += 1
            await sleep(policy.delay(retries))


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_seconds: float = 30.0
    clock: Clock = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _trial_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self.clock() - self._opened_at >= self.reset_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        """Whether a request may be sent now. In half-open, only one trial at a time."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self, error: ProviderError) -> None:
        if not error.counts_for_breaker:
            return
        was_trial = self._trial_in_flight
        self._trial_in_flight = False
        self._failures += 1
        if was_trial or self._failures >= self.failure_threshold:
            self._opened_at = self.clock()

    def release(self) -> None:
        """End a trial that ended without a health signal (for example, a bad request)."""
        self._trial_in_flight = False

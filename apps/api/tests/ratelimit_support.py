# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A stand-in for Valkey's side of ``pairly_core.ratelimit``: the token bucket script run
in Python on a clock the test controls, with failures and delays on demand."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any


class FakeValkey:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}
        self.calls = 0
        self.keys: list[str] = []
        self.fail: Exception | None = None
        self.delay = 0.0

    def register_script(self, script: str) -> Callable[..., Any]:
        assert "TIME" in script  # the server's clock, the same for every process
        return self._run

    async def _run(self, keys: list[str], args: list[Any]) -> list[int]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)
        capacity, rate, cost = int(args[0]), float(args[1]), int(args[2])
        now = self.clock()
        self.keys.append(keys[0])
        tokens, at = self.buckets.get(keys[0], (float(capacity), now))
        tokens = min(float(capacity), tokens + max(0.0, now - at) * rate)
        if cost <= capacity and tokens >= cost:
            self.buckets[keys[0]] = (tokens - cost, now)
            return [1, 0]
        self.buckets[keys[0]] = (tokens, now)
        return [0, max(1, math.ceil((min(cost, capacity) - tokens) / rate))]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

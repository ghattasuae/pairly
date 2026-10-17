# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Rate limits shared by every API process (security review 2026-10, L-03).

``pairly_core.ratelimit``: a token bucket per key in Valkey, the per-process limiters
as the fallback, the stricter of the two always wins. Two limiters with their own
local state stand for two API processes. The Valkey tests run against a real Valkey
when ``PAIRLY_TEST_REDIS_URL`` names one (the native test stack does), and are skipped
otherwise.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.datastructures import State

from pairly_core.api_keys.ratelimit import RateLimitedError, RateLimiter
from pairly_core.ratelimit import (
    SharedBuckets,
    SharedRateLimiter,
    SharedWindowLimiter,
    shared_buckets,
    window_limiters,
)
from pairly_hire.antispam import RateLimitedError as HireRateLimitedError
from pairly_hire.antispam import SlidingWindowLimiter, limiters

from .ratelimit_support import Clock, FakeValkey


def _processes(
    buckets: SharedBuckets, clock: Clock, n: int = 2, ns: str = "api_key"
) -> list[SharedRateLimiter]:
    return [
        SharedRateLimiter(ns, buckets=buckets, local=RateLimiter(clock=clock)) for _ in range(n)
    ]


async def _refused(limiter: SharedRateLimiter, key: str, per_minute: int) -> int:
    with pytest.raises(RateLimitedError) as refused:
        await limiter.check(key, per_minute)
    return refused.value.retry_after


# ------------------------------------------------------------------ one limit for all


async def test_two_api_processes_draw_from_one_bucket() -> None:
    clock = Clock()
    valkey = FakeValkey(clock)
    first, second = _processes(SharedBuckets(valkey, clock=clock), clock)
    await first.check("key-1", 3)
    await first.check("key-1", 3)
    await second.check("key-1", 3)
    # Each process has used at most two of its own three: only the shared bucket refuses.
    assert await _refused(second, "key-1", 3) >= 1
    assert await _refused(first, "key-1", 3) >= 1
    # Another key has its own bucket; the bucket refills at per_minute / 60 a second.
    await second.check("key-2", 3)
    clock.now += 20
    await second.check("key-1", 3)
    assert await _refused(first, "key-1", 3) >= 1


async def test_a_batch_cost_counts_against_the_shared_bucket() -> None:
    clock = Clock()
    first, second = _processes(
        SharedBuckets(FakeValkey(clock), clock=clock), clock, ns="batch_items"
    )
    await first.check("actor", 10, cost=7)
    with pytest.raises(RateLimitedError):
        await second.check("actor", 10, cost=4)
    await second.check("actor", 10, cost=3)
    with pytest.raises(RateLimitedError):
        await second.check("actor", 10, cost=11)  # more than the bucket ever holds


async def test_the_portal_and_public_form_windows_are_shared_too() -> None:
    clock = Clock()
    buckets = SharedBuckets(FakeValkey(clock), clock=clock)

    def process() -> SharedWindowLimiter:
        return SharedWindowLimiter(
            "hire.public.apply",
            SlidingWindowLimiter(2, 3600.0, clock=clock),
            buckets=buckets,
            error=HireRateLimitedError,
        )

    first, second = process(), process()
    await first.hit("198.51.100.9|acme")
    await second.hit("198.51.100.9|acme")
    with pytest.raises(HireRateLimitedError) as refused:
        await first.hit("198.51.100.9|acme")
    assert refused.value.retry_after >= 1
    with pytest.raises(HireRateLimitedError):
        await second.hit("198.51.100.9|acme")


async def test_the_stricter_of_the_two_limits_wins() -> None:
    clock = Clock()
    valkey = FakeValkey(clock)
    (process,) = _processes(SharedBuckets(valkey, clock=clock), clock, n=1)
    await process.check("k", 2)
    await process.check("k", 2)
    # The shared bucket alone is full again (flushed, say): this process's own bucket
    # still refuses.
    valkey.buckets.clear()
    assert await _refused(process, "k", 2) >= 1
    # The other way round: this process's bucket is fresh, the shared one is empty.
    first, second = _processes(SharedBuckets(valkey, clock=clock), clock)
    await first.check("k2", 1)
    assert await _refused(second, "k2", 1) >= 1


async def test_keys_never_reach_valkey_in_the_clear() -> None:
    clock = Clock()
    valkey = FakeValkey(clock)
    buckets = SharedBuckets(valkey, clock=clock)
    window = SharedWindowLimiter(
        "pay.vendor_portal.link_email",
        SlidingWindowLimiter(5, 3600.0, clock=clock),
        buckets=buckets,
    )
    await window.hit(f"{uuid.uuid4().hex}|layla.haddad@example.test")
    await window.hit("198.51.100.77")
    assert len(valkey.keys) == 2
    for key in valkey.keys:
        assert key.startswith("pairly:rl:pay.vendor_portal.link_email:")
        assert "example.test" not in key
        assert "198.51.100.77" not in key


# ------------------------------------------------------------------ Valkey down


async def test_valkey_down_leaves_the_local_limit_in_charge_and_trips_a_breaker() -> None:
    clock = Clock()
    valkey = FakeValkey(clock)
    valkey.fail = ConnectionError("connection refused")
    (process,) = _processes(SharedBuckets(valkey, clock=clock, cooldown=5.0), clock, n=1)
    await process.check("k", 2)
    await process.check("k", 2)
    assert await _refused(process, "k", 2) >= 1  # the local bucket refuses (fail closed)
    assert valkey.calls == 1  # the breaker: no call while it cools down
    clock.now += 31  # past the cooldown, and one token back in the local bucket
    valkey.fail = None
    await process.check("k", 2)  # Valkey answers again
    assert valkey.calls == 2


async def test_valkey_coming_back_keeps_counting_from_where_it_was() -> None:
    """Knocking Valkey over gives no fresh burst: the local bucket kept counting while
    Valkey answered, and decides alone while it is down."""
    clock = Clock()
    valkey = FakeValkey(clock)
    (process,) = _processes(SharedBuckets(valkey, clock=clock), clock, n=1)
    await process.check("k", 3)
    await process.check("k", 3)
    valkey.fail = OSError("gone")
    await process.check("k", 3)
    assert await _refused(process, "k", 3) >= 1


async def test_a_slow_valkey_times_out_and_the_local_limit_decides() -> None:
    clock = Clock()
    valkey = FakeValkey(clock)
    valkey.delay = 1.0
    (process,) = _processes(SharedBuckets(valkey, clock=clock, timeout=0.01), clock, n=1)
    started = asyncio.get_running_loop().time()
    await process.check("k", 1)
    assert asyncio.get_running_loop().time() - started < 0.5
    assert await _refused(process, "k", 1) >= 1


async def test_without_valkey_only_local_limits_apply() -> None:
    clock = Clock()
    buckets = SharedBuckets(None, clock=clock)
    assert not buckets.configured
    (process,) = _processes(buckets, clock, n=1)
    await process.check("k", 1)
    assert await _refused(process, "k", 1) >= 1
    window = SharedWindowLimiter("x", SlidingWindowLimiter(1, 60.0, clock=clock))
    await window.hit("ip")
    with pytest.raises(HireRateLimitedError):
        await window.hit("ip")


def test_every_route_set_gets_its_limiters_from_the_apps_shared_buckets() -> None:
    clock = Clock()
    buckets = SharedBuckets(FakeValkey(clock), clock=clock)
    app = FastAPI()
    app.state.rate_buckets = buckets

    class _Request:
        def __init__(self, app: FastAPI) -> None:
            self.app = app

    request: Any = _Request(app)
    assert shared_buckets(request) is buckets
    hire = limiters(request, "hire_limiters", {"apply": (10, 3600.0)}, "hire.public")
    assert hire["apply"].buckets is buckets
    assert hire["apply"].namespace == "hire.public.apply"
    assert limiters(request, "hire_limiters", {}, "ignored") is hire  # kept per app
    pay = window_limiters(
        request, "pay_x", {"open": (30, 3600.0)}, "pay.onboarding", local=SlidingWindowLimiter
    )
    assert pay["open"].limit == 30
    bare: Any = _Request(FastAPI())
    bare.app.state = State()
    assert not shared_buckets(bare).configured


# ------------------------------------------------------------------ a real Valkey

REDIS_URL = os.environ.get("PAIRLY_TEST_REDIS_URL")


@pytest.fixture
async def valkey() -> AsyncIterator[Any]:
    if not REDIS_URL:
        pytest.skip("PAIRLY_TEST_REDIS_URL is not set (no test Valkey)")
    from redis.asyncio import from_url

    clients = [from_url(REDIS_URL), from_url(REDIS_URL)]
    prefix = f"pairly:rl:test-{uuid.uuid4().hex[:8]}:"
    yield clients, prefix
    for key in await clients[0].keys(f"{prefix}*"):
        await clients[0].delete(key)
    for client in clients:
        await client.aclose()


async def test_on_a_real_valkey_two_processes_share_the_limit(valkey: Any) -> None:
    clients, prefix = valkey
    clock = Clock()
    first, second = (
        SharedRateLimiter(
            "api_key",
            buckets=SharedBuckets(client, prefix=prefix, timeout=2.0),
            local=RateLimiter(clock=clock),
        )
        for client in clients
    )
    await first.check("live-key", 3)
    await second.check("live-key", 3)
    await first.check("live-key", 3)
    assert await _refused(second, "live-key", 3) >= 1
    assert await _refused(first, "live-key", 3) >= 1
    (key,) = await clients[0].keys(f"{prefix}*")
    assert b"live-key" not in key
    ttl = await clients[0].pttl(key)
    assert 0 < ttl <= 61_000  # gone once the bucket would be full again


async def test_on_a_real_valkey_the_window_limit_is_shared(valkey: Any) -> None:
    clients, prefix = valkey
    clock = Clock()
    first, second = (
        SharedWindowLimiter(
            "hire.portal.link_ip",
            SlidingWindowLimiter(2, 3600.0, clock=clock),
            buckets=SharedBuckets(client, prefix=prefix, timeout=2.0),
            error=HireRateLimitedError,
        )
        for client in clients
    )
    await first.hit("203.0.113.5")
    await second.hit("203.0.113.5")
    with pytest.raises(HireRateLimitedError):
        await second.hit("203.0.113.5")


async def test_an_unreachable_valkey_fails_closed_to_the_local_limit() -> None:
    from redis.asyncio import from_url

    client = from_url("redis://127.0.0.1:9/0", socket_connect_timeout=0.2)
    try:
        clock = Clock()
        process = SharedRateLimiter(
            "api_key", buckets=SharedBuckets(client, timeout=1.0), local=RateLimiter(clock=clock)
        )
        await process.check("k", 1)
        assert await _refused(process, "k", 1) >= 1
    finally:
        await client.aclose()

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The in-memory request counters behind the platform operations error rate and latency."""

from __future__ import annotations

from pairly_api.request_metrics import WINDOW, RequestMetrics


def test_counts_and_percentiles() -> None:
    metrics = RequestMetrics()
    empty = metrics.snapshot()
    # No request yet: no rate and no latency, never a made-up zero.
    assert (empty.requests, empty.error_rate, empty.p50_ms) == (0, None, None)
    for ms in range(1, 101):
        metrics.record(200, float(ms))
    metrics.record(503, 1000.0)
    metrics.record(404, 5.0)
    snap = metrics.snapshot()
    assert (snap.requests, snap.errors_5xx, snap.errors_4xx) == (102, 1, 1)
    assert snap.error_rate == 1 / 102
    assert snap.p50_ms == 50.0  # nearest rank over 1..100 plus 5 and 1000
    assert snap.p99_ms == 100.0
    assert snap.source == "in_memory_request_counters"


def test_the_latency_window_is_bounded() -> None:
    metrics = RequestMetrics()
    for _ in range(WINDOW + 10):
        metrics.record(200, 1.0)
    snap = metrics.snapshot()
    assert snap.window == WINDOW
    assert snap.requests == WINDOW + 10

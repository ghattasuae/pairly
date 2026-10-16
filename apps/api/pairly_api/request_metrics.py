# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Request counters kept in memory by the edge middleware, for platform operations.

OpenTelemetry exports traces when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, but no metrics
are exported, so the operations dashboard reads these counters instead and labels them
as such: this API process only, since it started, lost on restart. They hold numbers
only: a status and a duration per request, no path, no tenant, no request content.
Health probes (``/healthz``, ``/readyz``) are not counted.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Final

WINDOW: Final = 2048  # the latencies kept for percentiles (the most recent ones)
SOURCE: Final = "in_memory_request_counters"


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    source: str
    since: float
    requests: int
    errors_5xx: int
    errors_4xx: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    window: int

    @property
    def error_rate(self) -> float | None:
        return self.errors_5xx / self.requests if self.requests else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return round(ordered[index], 2)


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = time.time()
        self._requests = 0
        self._errors_5xx = 0
        self._errors_4xx = 0
        self._latencies: deque[float] = deque(maxlen=WINDOW)

    def record(self, status: int, duration_ms: float) -> None:
        with self._lock:
            self._requests += 1
            if status >= 500:
                self._errors_5xx += 1
            elif status >= 400:
                self._errors_4xx += 1
            self._latencies.append(duration_ms)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            latencies = list(self._latencies)
            return MetricsSnapshot(
                source=SOURCE,
                since=self._since,
                requests=self._requests,
                errors_5xx=self._errors_5xx,
                errors_4xx=self._errors_4xx,
                p50_ms=_percentile(latencies, 50),
                p95_ms=_percentile(latencies, 95),
                p99_ms=_percentile(latencies, 99),
                window=len(latencies),
            )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The detector registry and the fail-closed runner."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

import structlog

from pairly_core.llm.errors import ModelUnavailable
from pairly_core.risk.types import DetectorTools, RiskContext, RiskDetector, RiskSignal

log = structlog.get_logger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}$")


class DetectorError(ValueError):
    """A detector is malformed or registered twice (a programming error)."""


class DetectorRegistry:
    """Detectors by name. Core registers the generic ones; workflows add their own."""

    def __init__(self, detectors: Iterable[RiskDetector] = ()) -> None:
        self._detectors: dict[str, RiskDetector] = {}
        for detector in detectors:
            self.register(detector)

    def register(self, detector: RiskDetector) -> None:
        name = detector.name
        if not _NAME_RE.fullmatch(name):
            raise DetectorError(f"invalid detector name {name!r}")
        if name in self._detectors:
            raise DetectorError(f"duplicate detector {name!r}")
        self._detectors[name] = detector

    def get(self, name: str) -> RiskDetector | None:
        return self._detectors.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._detectors)

    def select(self, names: Sequence[str] | None = None) -> list[RiskDetector]:
        """The named detectors (all when ``names`` is None). Unknown names raise."""
        if names is None:
            return list(self._detectors.values())
        missing = [n for n in names if n not in self._detectors]
        if missing:
            raise DetectorError(f"unknown detectors {missing}")
        return [self._detectors[n] for n in names]


async def run_detector(
    detector: RiskDetector, ctx: RiskContext, tools: DetectorTools
) -> RiskSignal:
    """Run one detector. Never raises: a failure is an ``error`` signal (fail closed)."""
    name, version = detector.name, detector.version
    if detector.needs_model and tools.router is None:
        return RiskSignal.error(name, version, "model_router_missing")
    try:
        signal = await detector.detect(ctx, tools)
    except ModelUnavailable as exc:
        log.warning("risk.detector_model_unavailable", detector=name, code=exc.code)
        return RiskSignal.error(name, version, f"model_unavailable:{exc.code}")
    except Exception as exc:
        log.error("risk.detector_failed", detector=name, error=type(exc).__name__)
        return RiskSignal.error(name, version, "detector_failed")
    if not isinstance(signal, RiskSignal) or signal.detector != name:
        return RiskSignal.error(name, version, "invalid_signal")
    return signal


async def run_detectors(
    detectors: Sequence[RiskDetector], ctx: RiskContext, tools: DetectorTools
) -> list[RiskSignal]:
    """Run detectors in order (deterministic output order)."""
    return [await run_detector(d, ctx, tools) for d in detectors]

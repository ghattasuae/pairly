# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Built-in guardrail detectors and the default registry.

Add a detector with ``registry.register(MyDetector())``: any object with a ``name`` and
a ``detect(scan: ScanInput) -> Iterable[Finding]`` method.
"""

from __future__ import annotations

from pairly_core.guardrails.base import DetectorRegistry
from pairly_core.guardrails.detectors.hidden import HiddenTextDetector
from pairly_core.guardrails.detectors.instructions import InstructionDetector, MetadataDetector
from pairly_core.guardrails.detectors.stuffing import KeywordStuffingDetector
from pairly_core.guardrails.detectors.unicode import UnicodeDetector


def default_registry() -> DetectorRegistry:
    return DetectorRegistry(
        [
            HiddenTextDetector(),
            InstructionDetector(),
            KeywordStuffingDetector(),
            MetadataDetector(),
            UnicodeDetector(),
        ]
    )


__all__ = [
    "HiddenTextDetector",
    "InstructionDetector",
    "KeywordStuffingDetector",
    "MetadataDetector",
    "UnicodeDetector",
    "default_registry",
]

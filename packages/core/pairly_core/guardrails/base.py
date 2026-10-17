# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Detector interface, configuration and scan context shared by every detector."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pairly_core.documents.model import BBox, NormalizedDocument
from pairly_core.guardrails.explain import DocKind
from pairly_core.guardrails.report import Finding
from pairly_core.guardrails.textindex import TextIndex

MAX_EVIDENCE_CHARS = 300


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    """Thresholds. Defaults are tuned on the synthetic attack corpus (``synth.attacks``)."""

    # WCAG contrast ratio between text and background below which text is hidden.
    min_contrast: float = 1.5
    # Below this ratio the finding is high severity (white on white is 1.0).
    high_contrast_below: float = 1.2
    # Font sizes below this (points) are unreadable; below the second, high severity.
    min_font_pt: float = 4.0
    # Renderer check: a covered or clipped run needs this many visible characters.
    render_min_chars: int = 3
    tiny_font_high_below_pt: float = 2.5
    # Keyword stuffing score (0..1): low from the first, flagged from the second.
    stuffing_low_score: float = 0.3
    stuffing_flag_score: float = 0.5
    stuffing_high_score: float = 0.8
    # Free text in file properties longer than this is reported (low severity).
    metadata_text_chars: int = 300


@dataclass(frozen=True, slots=True)
class ScanContext:
    """Where the document came from: checked for instructions too, never sent to a model."""

    filename: str | None = None
    email_subject: str | None = None
    email_headers: Mapping[str, str] = field(default_factory=dict)
    doc_kind: DocKind = "document"


@dataclass(frozen=True, slots=True)
class ScanInput:
    doc: NormalizedDocument
    text: str
    index: TextIndex
    config: GuardrailConfig
    context: ScanContext


@runtime_checkable
class Detector(Protocol):
    """A deterministic check. ``detect`` yields findings with ``id=""`` (set by the scan)."""

    name: str

    def detect(self, scan: ScanInput) -> Iterable[Finding]: ...


class DetectorRegistry:
    """Ordered, named detectors. Plugins may add their own."""

    def __init__(self, detectors: Iterable[Detector] = ()) -> None:
        self._detectors: dict[str, Detector] = {}
        for detector in detectors:
            self.register(detector)

    def register(self, detector: Detector, *, replace: bool = False) -> None:
        if not isinstance(detector, Detector):
            raise TypeError("a detector needs a name and a detect(scan) method")
        if detector.name in self._detectors and not replace:
            raise ValueError(f"detector {detector.name!r} is already registered")
        self._detectors[detector.name] = detector

    def unregister(self, name: str) -> None:
        self._detectors.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._detectors)

    def __iter__(self) -> Iterator[Detector]:
        return iter(tuple(self._detectors.values()))


def evidence(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def union_bbox(boxes: Iterable[BBox]) -> BBox | None:
    items = list(boxes)
    if not items:
        return None
    return BBox(
        x0=min(b.x0 for b in items),
        top=min(b.top for b in items),
        x1=max(b.x1 for b in items),
        bottom=max(b.bottom for b in items),
    )


def sentence_bounds(text: str, start: int, end: int, limit: int = 300) -> tuple[int, int]:
    """Expand ``[start, end)`` to the sentence (or line) around it, within ``limit``."""
    stops = "\n.!?\u061f\u06d4"
    left = start
    floor = max(0, start - limit)
    while left > floor and text[left - 1] not in stops:
        left -= 1
    right = end
    ceiling = min(len(text), end + limit)
    while right < ceiling and text[right] not in stops:
        right += 1
    if right < len(text) and text[right] in ".!?\u061f\u06d4":
        right += 1
    while left < start and text[left].isspace():
        left += 1
    while right > end and text[right - 1].isspace():
        right -= 1
    return left, right

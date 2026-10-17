# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The vision-model fallback for scans that OCR could not read well.

``parse_document`` calls a ``VisionFallback`` when the OCR mean confidence is below
``PAIRLY_OCR_MIN_CONFIDENCE``. The implementation will call a vision model through the
model router (WP1.7); none ships here. Without one, or when it fails or returns
nothing, the document keeps its OCR text, gets a ``low_ocr_confidence`` warning and is
sent to the human queue (fail closed).

It runs in the worker, outside the parser sandbox, because it needs the network.
TODO(WP1.4): route the page images through the guardrails before any model sees them.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pairly_core.documents.model import NormalizedDocument


@runtime_checkable
class VisionFallback(Protocol):
    async def transcribe(
        self,
        *,
        path: Path,
        media_type: str,
        document: NormalizedDocument,
        pages: Sequence[int],
    ) -> NormalizedDocument | None:
        """Return an improved document for ``pages`` (1-based), or None to keep OCR."""
        ...

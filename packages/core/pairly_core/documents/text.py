# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Plain text (email bodies stored by inbound email). One logical page, one block per line."""

from __future__ import annotations

from pathlib import Path

from pairly_core.documents.model import (
    NormalizedDocument,
    Page,
    ParserInfo,
    ParseWarning,
    TextBlock,
)
from pairly_core.documents.registry import ParseOptions
from pairly_core.ingestion.sniff import TEXT

MAX_TEXT_BYTES = 8 * 1024 * 1024


class TextParser:
    name = "text"
    version = "1"
    media_types = frozenset({TEXT})

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        with path.open("rb") as handle:
            data = handle.read(MAX_TEXT_BYTES + 1)
        warnings: list[ParseWarning] = []
        if len(data) > MAX_TEXT_BYTES:
            data = data[:MAX_TEXT_BYTES]
            warnings.append(ParseWarning(code="chars_truncated", message="text truncated"))
        text = data.decode("utf-8", errors="replace")
        blocks = [
            TextBlock(text=line, source="text", group=index)
            for index, line in enumerate(text.splitlines())
            if line.strip()
        ]
        if not blocks:
            warnings.append(ParseWarning(code="no_text", message="no text found"))
        return NormalizedDocument(
            media_type=TEXT,
            parser=ParserInfo(name=self.name, version=self.version),
            pages=[Page(number=1, width=0.0, height=0.0, unit="pt", text=text, blocks=blocks)],
            warnings=warnings,
        )

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic checks of job text, English and Arabic (brief 3.1.2).

* ``inclusive``: wording that is gendered, ageist, exclusionary or jargon, with
  alternatives (wordlists and patterns, no model).
* ``sanity``: requirement combinations that cannot be met, such as more years of a
  technology than it has existed (a small curated table) or contradictory seniority.

Both return findings with character offsets into the checked text, so the editor can
highlight them. They never change the text: a person decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    category: str
    severity: str  # "info" or "warning"
    start: int
    end: int
    match: str
    message: str
    suggestions: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "category": self.category,
            "severity": self.severity,
            "start": self.start,
            "end": self.end,
            "match": self.match,
            "message": self.message,
            "suggestions": list(self.suggestions),
        }

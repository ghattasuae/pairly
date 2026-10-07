# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Keyword stuffing: text written for a keyword matcher, not for a reader.

Two scored signals (0 to 1), the larger one is the score:

* repetition: one content word takes an unusual share of all words
  (``min(1, share / 8%) * min(1, count / 15)``, from 8 occurrences);
* bare lists: consecutive lines that are only short comma, pipe or bullet separated
  items, with no sentences around them (``(items - 20) / 30``, plus 0.3 when a quarter
  or more of the items repeat).

A normal skills line (10 to 20 items) scores 0. Findings are not removed from model
input (the words are visible); scoring must rely on quoted evidence instead.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from pairly_core.guardrails.base import ScanInput, evidence
from pairly_core.guardrails.report import Finding, FindingType, Severity, Span
from pairly_core.guardrails.textnorm import fold_for_matching

_WORD = re.compile(r"[^\W\d_][\w+#]*(?:[.-][\w+#]+)*")
_SEPARATORS = re.compile(r"\s*(?:[,|;\u2022\u00b7\u060c/]|\s{3,})\s*")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "will",
        "with",
        "you",
        "your",
        "my",
        "me",
        "not",
        "no",
        "so",
        "than",
        "then",
        "there",
        "these",
        "those",
        "which",
        "who",
        "what",
        "when",
        "where",
        "how",
        "all",
        "any",
        "can",
        "could",
        "would",
        "should",
        "may",
        "might",
        "do",
        "does",
        "did",
        "into",
        "over",
        "under",
        "about",
        "after",
        "before",
        "في",
        "من",
        "على",
        "الى",
        "عن",
        "مع",
        "هذا",
        "هذه",
        "ذلك",
        "التي",
        "الذي",
        "و",
        "او",
        "ثم",
        "كما",
        "ان",
        "كان",
    ]
)


def _score_repetition(words: list[str]) -> tuple[float, str, int]:
    content = [w for w in words if len(w) >= 2 and w not in STOPWORDS]
    if len(words) < 30 or not content:
        return 0.0, "", 0
    term, count = Counter(content).most_common(1)[0]
    if count < 8:
        return 0.0, term, count
    share = count / len(words)
    return min(1.0, share / 0.08) * min(1.0, count / 15), term, count


def _list_items(line: str) -> list[str] | None:
    items = [i for i in _SEPARATORS.split(line.strip()) if i.strip()]
    if len(items) < 4:
        return None
    if sum(len(i.split()) for i in items) / len(items) > 3:
        return None
    return items


class KeywordStuffingDetector:
    name = "keyword_stuffing"

    def detect(self, scan: ScanInput) -> Iterable[Finding]:
        text = scan.text
        cfg = scan.config
        folded = fold_for_matching(text)
        words = [m.group() for m in _WORD.finditer(folded.text)]
        rep_score, term, count = _score_repetition(words)
        if rep_score >= cfg.stuffing_low_score:
            yield self._finding(
                scan,
                "repetition",
                rep_score,
                f'"{term}" x{count} in {len(words)} words',
                (),
                {"term": term, "count": count, "words": len(words)},
            )

        best: tuple[float, int, int, int] | None = None
        run_start = run_end = -1
        run_items: list[str] = []
        offset = 0

        def close() -> None:
            nonlocal best, run_items
            if run_items:
                distinct = len({i.casefold() for i in run_items})
                repeats = 1 - distinct / len(run_items)
                score = max(0.0, min(1.0, (len(run_items) - 20) / 30))
                if repeats >= 0.25 and len(run_items) >= 12:
                    score = min(1.0, score + 0.3)
                if best is None or score > best[0]:
                    best = (score, run_start, run_end, len(run_items))
            run_items = []

        for line in text.split("\n"):
            items = _list_items(line)
            if items is None:
                if line.strip():
                    close()
            else:
                if not run_items:
                    run_start = offset
                run_items.extend(items)
                run_end = offset + len(line)
            offset += len(line) + 1
        close()
        if best is not None and best[0] >= cfg.stuffing_low_score:
            score, start, end, items_count = best
            yield self._finding(
                scan,
                "skill_list",
                score,
                text[start:end].strip(),
                (Span(start=start, end=end),),
                {"items": items_count},
            )

    def _finding(
        self,
        scan: ScanInput,
        code: str,
        score: float,
        found: str,
        spans: tuple[Span, ...],
        details: dict[str, str | int | float | bool],
    ) -> Finding:
        cfg = scan.config
        if score >= cfg.stuffing_high_score:
            severity = Severity.HIGH
        elif score >= cfg.stuffing_flag_score:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW
        return Finding(
            id="",
            type=FindingType.KEYWORD_STUFFING,
            code=code,
            severity=severity,
            detector=self.name,
            evidence=evidence(found),
            page=scan.index.page_of(spans[0].start) if spans else None,
            spans=spans,
            remove=False,
            score=round(score, 3),
            details=details,
        )

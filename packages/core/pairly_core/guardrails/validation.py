# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Output validation for model results: schema, ranges, banned content, evidence.

* ``validate_output(schema, data, ranges=..., banned=...)`` validates with Pydantic,
  then checks numeric ranges by dotted path and scans every string for banned content.
  ``OutputCheck.ok`` is False on any issue: the caller routes the item to a person.
* ``quote_exists(original_doc, quote, sanitized=...)`` is the "no evidence, no credit"
  check: the quote must appear in the document after whitespace, quote, dash and
  Unicode normalisation (case is kept). With ``sanitized``, it must appear in the text
  the model was allowed to see, so a quote taken from hidden text never counts.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from pairly_core.documents.model import NormalizedDocument
from pairly_core.guardrails.report import Span
from pairly_core.guardrails.sanitize import DATA_TAG, ModelInput
from pairly_core.guardrails.textnorm import fold_for_quotes

MIN_QUOTE_CHARS = 3


# ------------------------------------------------------------------ evidence quotes


def _normalized_quote(quote: str) -> str:
    return fold_for_quotes(quote).text


def find_quote(source: str, quote: str) -> list[Span]:
    """Every occurrence of ``quote`` in ``source``, as spans of ``source``."""
    needle = _normalized_quote(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return []
    folded = fold_for_quotes(source)
    spans: list[Span] = []
    pos = folded.text.find(needle)
    while pos >= 0:
        start, end = folded.span(pos, pos + len(needle))
        spans.append(Span(start=start, end=end))
        pos = folded.text.find(needle, pos + 1)
    return spans


def locate_quote(
    original_doc: NormalizedDocument | str, quote: str, *, sanitized: ModelInput | None = None
) -> list[Span]:
    """Spans of ``doc.text`` where ``quote`` is found (in the sanitised text if given)."""
    if sanitized is None:
        text = original_doc if isinstance(original_doc, str) else original_doc.text
        return find_quote(text, quote)
    spans: list[Span] = []
    for hit in find_quote(sanitized.text, quote):
        spans.extend(sanitized.to_original(hit.start, hit.end))
    return spans


def quote_exists(
    original_doc: NormalizedDocument | str, quote: str, *, sanitized: ModelInput | None = None
) -> bool:
    return bool(locate_quote(original_doc, quote, sanitized=sanitized))


# ------------------------------------------------------------------ banned content


@dataclass(frozen=True, slots=True)
class BannedRule:
    code: str
    regex: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class BannedContentPolicy:
    rules: tuple[BannedRule, ...]

    def extend(self, *rules: BannedRule) -> BannedContentPolicy:
        return BannedContentPolicy((*self.rules, *rules))


def _rule(code: str, pattern: str) -> BannedRule:
    return BannedRule(code, re.compile(pattern, re.IGNORECASE))


# Default: what a model output should never contain, whatever the task.
DEFAULT_POLICY = BannedContentPolicy(
    (
        _rule("markup", r"<\s*(?:script|iframe|object|embed|form|img|svg|style)\b|javascript\s*:"),
        _rule("markdown_image", r"!\[[^\]]{0,200}\]\("),
        _rule("data_uri", r"\bdata:[a-z]+/[a-z0-9.+-]+;base64,"),
        _rule("prompt_leak", r"</?\s*" + DATA_TAG + r"\b|\bsystem\s+prompt\b"),
        _rule(
            "action_claim",
            r"\b(?:i\s+have|i've)\s+(?:approved|paid|rejected|sent|deleted|changed)\b",
        ),
    )
)

# For hiring rationales and summaries: no protected attributes as reasons.
HIRING_POLICY = DEFAULT_POLICY.extend(
    _rule(
        "protected_attribute",
        r"\b(?:age|aged|years\s+old|gender|male|female|pregnan\w*|religio\w*|muslim|christian|"
        r"hindu|jewish|nationality|ethnic\w*|race|racial|marital|married|single\s+mother|"
        r"disabilit\w*|disabled)\b|(?:العمر|الجنس|الديانه|الديانة|الجنسيه|الجنسية|الحاله\s+"
        r"الاجتماعيه|الحالة\s+الاجتماعية|متزوج|حامل|اعاقه|إعاقة)",
    ),
)


@dataclass(frozen=True, slots=True)
class OutputIssue:
    path: str
    code: str
    message: str


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "$", value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def check_banned_content(
    value: Any, policy: BannedContentPolicy = DEFAULT_POLICY
) -> list[OutputIssue]:
    issues: list[OutputIssue] = []
    for path, text in _strings(value):
        for rule in policy.rules:
            if rule.regex.search(text):
                issues.append(
                    OutputIssue(path, f"banned:{rule.code}", f"banned content ({rule.code})")
                )
    return issues


def _lookup(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    """Values at a dotted path; ``[]`` fans out over list items (``criteria[].score``)."""
    parts = path.split(".")

    def walk(current: Any, index: int, trail: str) -> Iterator[tuple[str, Any]]:
        if index == len(parts):
            yield trail, current
            return
        part = parts[index]
        fan = part.endswith("[]")
        name = part[:-2] if fan else part
        child = current.get(name) if isinstance(current, Mapping) else getattr(current, name, None)
        here = f"{trail}.{name}" if trail else name
        if fan:
            for i, item in enumerate(child if isinstance(child, list | tuple) else []):
                yield from walk(item, index + 1, f"{here}[{i}]")
        elif child is not None:
            yield from walk(child, index + 1, here)

    yield from walk(value, 0, "")


def check_ranges(
    value: Any, ranges: Mapping[str, tuple[float | None, float | None]]
) -> list[OutputIssue]:
    issues: list[OutputIssue] = []
    for path, (low, high) in ranges.items():
        for where, number in _lookup(value, path):
            if isinstance(number, bool) or not isinstance(number, int | float):
                issues.append(OutputIssue(where, "range:type", "expected a number"))
                continue
            if (
                number != number
                or (low is not None and number < low)
                or (high is not None and number > high)
            ):
                issues.append(OutputIssue(where, "range:out_of_bounds", f"{number} not in range"))
    return issues


@dataclass(frozen=True, slots=True)
class OutputCheck[T: BaseModel]:
    value: T | None
    issues: tuple[OutputIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.issues


def validate_output[T: BaseModel](
    schema: type[T],
    data: Any,
    *,
    ranges: Mapping[str, tuple[float | None, float | None]] | None = None,
    banned: BannedContentPolicy | None = DEFAULT_POLICY,
) -> OutputCheck[T]:
    """Schema first; ranges and banned content only on a valid object (fail closed)."""
    try:
        value = data if isinstance(data, schema) else schema.model_validate(data)
    except ValidationError as exc:
        issues = tuple(
            OutputIssue(".".join(str(p) for p in err["loc"]) or "$", "schema", err["msg"])
            for err in exc.errors()
        )
        return OutputCheck(None, issues)
    dumped = value.model_dump(mode="json")
    issues_list = check_ranges(dumped, ranges or {})
    if banned is not None:
        issues_list.extend(check_banned_content(dumped, banned))
    return OutputCheck(value, tuple(issues_list))


def check_evidence(
    quotes: Sequence[tuple[str, str]],
    original_doc: NormalizedDocument | str,
    *,
    sanitized: ModelInput | None = None,
) -> list[OutputIssue]:
    """``(path, quote)`` pairs whose quote is missing from the document: no credit."""
    return [
        OutputIssue(path, "evidence:not_found", "quoted evidence is not in the document")
        for path, quote in quotes
        if not quote_exists(original_doc, quote, sanitized=sanitized)
    ]

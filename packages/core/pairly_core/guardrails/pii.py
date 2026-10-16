# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""PII redaction for logs, traces and LangFuse.

``PiiRedactor`` replaces personal data with a typed placeholder:

    [EMAIL] [PHONE] [IBAN] [CARD] [EMIRATES_ID] [PASSPORT] [NAME]

* emails;
* IBANs (ISO 13616 shape: country, check digits, 11 to 30 letters or digits, with or
  without spaces every four characters);
* payment card numbers, 13 to 19 digits, only when the Luhn check passes;
* Emirates ID numbers (``784-YYYY-NNNNNNN-C``, with or without separators);
* passport numbers after a "passport" or Arabic "جواز" label, and the common one letter
  plus seven or eight digits shape;
* phone numbers: international (``+`` or ``00``), UAE mobile (``05X XXX XXXX``) and
  landline (``0X XXX XXXX``) formats, and any number after a phone label (tel, mobile,
  WhatsApp, هاتف, جوال);
* names from a list the caller provides (for example a candidate's own name).

It is tuned to over-redact rather than leak: a log line loses a number it did not need
before a person's data reaches a log store. ``redact`` recurses into mappings, lists
and tuples (keys are kept). It is the default ``Redactor`` of the model router (LangFuse
inputs and outputs) and backs ``redact_log_event``, a structlog processor.

``redact_counted`` also returns how many values it masked. The log processor and the
LangFuse tracer add that number to the organisation's tally
(``pairly_core.guardrails.redaction_counts``), so the AI governance board shows how much
personal data was kept out of logs and traces. Only the number is kept, never the text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

_EMAIL = re.compile(r"(?<![\w.+-])[\w.%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
_EMIRATES_ID = re.compile(r"(?<!\d)784[- ]?\d{4}[- ]?\d{7}[- ]?\d(?!\d)")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_PASSPORT_LABEL = re.compile(
    r"(?i:passport(?:\s*(?:no\.?|number|num|#))?|جواز(?:\s+السفر)?(?:\s+رقم)?)\s*[:#]?\s*"
    r"([A-Z0-9]{6,9})\b"
)
_PASSPORT = re.compile(r"\b[A-Z]\d{7,8}\b")
_PHONE_LABEL = re.compile(
    r"(?i:\b(?:tel|telephone|phone|mobile|mob|cell|whatsapp|fax)|هاتف|جوال|موبايل|واتساب)"
    r"\.?\s*[:#]?\s*(\+?\d[\d\s().-]{6,20}\d)"
)
_PHONE_INTL = re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){1,5}(?![\w])")
_PHONE_UAE = re.compile(r"(?<![\w+])0(?:5[0-9]|[2-79])[\s-]?\d{3}[\s-]?\d{4}(?!\d)")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True, slots=True)
class PiiMatch:
    kind: str
    start: int
    end: int


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


class PiiRedactor:
    """Masks personal data in text and JSON-like values. Thread safe, no state."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        cleaned = sorted({n.strip() for n in names if len(n.strip()) >= 2}, key=len, reverse=True)
        self._names = (
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(n) for n in cleaned) + r")(?!\w)", re.IGNORECASE
            )
            if cleaned
            else None
        )

    def with_names(self, names: Iterable[str]) -> PiiRedactor:
        return PiiRedactor(names)

    def find(self, text: str) -> list[PiiMatch]:
        """Non-overlapping matches; earlier kinds in the list below win overlaps."""
        found: list[PiiMatch] = []

        def add(kind: str, start: int, end: int) -> None:
            if any(start < m.end and m.start < end for m in found):
                return
            found.append(PiiMatch(kind, start, end))

        for m in _EMAIL.finditer(text):
            add("EMAIL", m.start(), m.end())
        for m in _IBAN.finditer(text):
            add("IBAN", m.start(), m.end())
        for m in _EMIRATES_ID.finditer(text):
            add("EMIRATES_ID", m.start(), m.end())
        for m in _CARD.finditer(text):
            digits = _digits(m.group())
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                add("CARD", m.start(), m.end())
        for m in _PASSPORT_LABEL.finditer(text):
            add("PASSPORT", m.start(1), m.end(1))
        for m in _PASSPORT.finditer(text):
            add("PASSPORT", m.start(), m.end())
        for m in _PHONE_LABEL.finditer(text):
            add("PHONE", m.start(1), m.end(1))
        for m in _PHONE_INTL.finditer(text):
            if 8 <= len(_digits(m.group())) <= 15:
                add("PHONE", m.start(), m.end())
        for m in _PHONE_UAE.finditer(text):
            add("PHONE", m.start(), m.end())
        if self._names is not None:
            for m in self._names.finditer(text):
                add("NAME", m.start(), m.end())
        return sorted(found, key=lambda m: m.start)

    def _redact_text(self, text: str) -> tuple[str, int]:
        matches = self.find(text)
        if not matches:
            return text, 0
        out: list[str] = []
        position = 0
        for match in matches:
            out.append(text[position : match.start])
            out.append(f"[{match.kind}]")
            position = match.end
        out.append(text[position:])
        return "".join(out), len(matches)

    def redact_text(self, text: str) -> str:
        return self._redact_text(text)[0]

    def redact_counted(self, value: Any) -> tuple[Any, int]:
        """``redact(value)`` and the number of values it masked."""
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            total = 0
            mapped: dict[Any, Any] = {}
            for k, v in value.items():
                mapped[k], n = self.redact_counted(v)
                total += n
            return mapped, total
        if isinstance(value, list | tuple):
            total = 0
            items: list[Any] = []
            for v in value:
                item, n = self.redact_counted(v)
                items.append(item)
                total += n
            return items, total
        return value, 0

    def redact(self, value: Any) -> Any:
        return self.redact_counted(value)[0]


# Structured log fields that never hold personal data and must stay searchable.
SAFE_LOG_KEYS = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "logger_name",
        "trace_id",
        "span_id",
        "request_id",
        "org_id",
        "user_id",
        "document_id",
        "call_id",
        "chain_id",
        "status",
        "method",
        "path",
        "duration_ms",
    }
)

_DEFAULT = PiiRedactor()


def redact_log_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact PII in every value except ``SAFE_LOG_KEYS``.

    Put it after ``format_exc_info`` so exception text is redacted too. What it masks is
    counted for the event's ``org_id`` (bound per request), when there is one.
    """
    masked = 0
    for key, value in list(event_dict.items()):
        if key in SAFE_LOG_KEYS or key.startswith("_"):
            continue
        event_dict[key], n = _DEFAULT.redact_counted(value)
        masked += n
    if masked:
        # Imported here: the tally module needs the database layer, this one does not.
        from pairly_core.guardrails.redaction_counts import TALLY

        TALLY.add(event_dict.get("org_id"), masked)
    return event_dict

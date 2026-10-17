# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Which inbound emails and attachments are invoices (brief 4.2.3). Pure rules, no I/O.

``classify_email`` sorts a parsed email into:

* ``invoice`` (it carries an invoice: an attachment, or invoice words in the body or
  subject);
* ``marketing`` with high confidence (no invoice signal at all, and bulk mail headers
  or an unsubscribe line or newsletter words): ignored, no receipt;
* ``unsure``: no invoice signal and no marketing signal either. Never dropped: the
  email body becomes an invoice in ``needs_review`` so a person decides (fail closed).

``classify_attachment`` tells invoices from the files that travel with them (terms and
conditions, logos, brochures) by name and type. Only a clear supporting file is set
aside (kept with the email, not read as an invoice); anything unclear is read as an
invoice and a person sees what the extraction found.

Rules, not a model: they are cheap, explainable and tested on the synthetic emails. A
model-based classifier can replace them behind the same result type later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

from pairly_core.ingestion.email import ParsedEmail

Kind = Literal["invoice", "marketing", "unsure"]
Confidence = Literal["high", "low"]

_INVOICE_WORDS: Final = re.compile(
    r"\b(?:tax\s+invoice|invoice|inv\s*(?:no|#)|amount\s+due|total\s+due|"
    r"payment\s+due|bill\s+to|credit\s+note|statement\s+of\s+account)\b|فاتورة|ضريبية",
    re.IGNORECASE,
)
_MARKETING_WORDS: Final = re.compile(
    r"\b(?:newsletter|catalogue|catalog|webinar|unsubscribe|promotion|promo|offer|offers|"
    r"discount|save\s+\d+\s*%|sale|register\s+(?:now|for)|invitation|award|digest|"
    r"product\s+update|what'?s\s+new|opening\s+hours|subscribe)\b",
    re.IGNORECASE,
)
_UNSUBSCRIBE: Final = re.compile(r"\bunsubscribe\b|إلغاء الاشتراك", re.IGNORECASE)
_SUPPORTING_NAMES: Final = re.compile(
    r"(?:terms|conditions|t&c|tnc|logo|signature|brochure|catalog(?:ue)?|profile|"
    r"newsletter|banner|price[\s_-]?list|company[\s_-]?profile)",
    re.IGNORECASE,
)
_INVOICE_NAMES: Final = re.compile(r"(?:invoice|inv[\s_-]|bill|فاتورة)", re.IGNORECASE)
_IMAGE_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/tiff"})
# Images this small are logos and signatures, not scanned invoices.
SMALL_IMAGE_BYTES: Final = 20 * 1024


@dataclass(frozen=True, slots=True)
class EmailVerdict:
    kind: Kind
    confidence: Confidence
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ignored(self) -> bool:
        return self.kind == "marketing" and self.confidence == "high"


def classify_email(parsed: ParsedEmail) -> EmailVerdict:
    subject = parsed.subject or ""
    body = parsed.text_body or ""
    invoice: list[str] = []
    if any(
        _classify_name(a.filename, a.declared_type, len(a.data)) != "supporting"
        for a in parsed.attachments
    ):
        invoice.append("attachment")
    if _INVOICE_WORDS.search(subject):
        invoice.append("subject_invoice_words")
    if _INVOICE_WORDS.search(body):
        invoice.append("body_invoice_words")
    if invoice:
        return EmailVerdict("invoice", "high", tuple(invoice))

    marketing: list[str] = []
    if parsed.is_bulk:
        marketing.append("bulk_headers")
    if _UNSUBSCRIBE.search(body):
        marketing.append("unsubscribe_line")
    if _MARKETING_WORDS.search(subject):
        marketing.append("marketing_subject")
    elif _MARKETING_WORDS.search(body):
        marketing.append("marketing_body")
    if marketing:
        return EmailVerdict("marketing", "high", tuple(marketing))
    return EmailVerdict("unsure", "low", ("no_invoice_signal",))


AttachmentKind = Literal["invoice", "supporting", "unclear"]


def _classify_name(filename: str | None, media_type: str | None, size: int) -> AttachmentKind:
    name = filename or ""
    if _INVOICE_NAMES.search(name):
        return "invoice"
    if _SUPPORTING_NAMES.search(name):
        return "supporting"
    if (media_type or "") in _IMAGE_TYPES and 0 < size <= SMALL_IMAGE_BYTES:
        return "supporting"
    return "unclear"


def classify_attachment(filename: str | None, media_type: str | None, size: int) -> AttachmentKind:
    """``supporting`` only when the name or a tiny image says so; else read it."""
    return _classify_name(filename, media_type, size)

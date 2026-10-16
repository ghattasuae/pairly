# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Fake or generated invoice signals (brief 4.4.6). Flags only: nothing is blocked or
opened here; strong signals send the invoice to a person through the risk score.

* ``template_mismatch``: the document's fingerprint (``fingerprint``: font families,
  accent colours, logo, numbering pattern, billing software, page size) against the
  vendor's earlier documents of the same type. A feature counts when the vendor is
  consistent about it (two thirds of at least ``MIN_TEMPLATES`` earlier documents agree)
  and this one differs. Two features: medium; three or more: high.
* ``metadata_inconsistency``: created after it was modified, made with a generic editor
  or invoice generator, a PDF created long after the invoice date (word processor dates
  are template dates, so DOCX files are not compared), a title naming another
  invoice number, a generic author account.
* ``editing_traces``: text printed over other text, figures under a white box, or a
  guardrail "covered" finding on a figure.
* ``trn_invalid`` / ``trn_mismatch``: the printed TRN is not 15 digits, or differs from
  the vendor master.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any, Final

from pairly_core.guardrails.report import FindingType
from pairly_core.risk.types import Severity
from pairly_pay.checks.duplicates import number_key
from pairly_pay.checks.messages import terms
from pairly_pay.checks.settings import CheckSettings
from pairly_pay.checks.types import (
    CheckResult,
    DocumentFacts,
    Finding,
    History,
    InvoiceFacts,
    finding,
    result,
)

CHECK: Final = "forgery"
VERSION: Final = "1"
MIN_TEMPLATES: Final = 3
FEATURES: Final = ("fonts", "accents", "images", "numbering", "creator", "page_size")
GENERIC_TOOLS: Final = re.compile(
    r"pdf\s*editor|\beditor\b|generator|invoice\s*maker|online\s*invoice|photoshop|\bgimp\b"
    r"|\bcanva\b|ilovepdf|smallpdf|sejda|pdfescape|pdf24|soda\s*pdf|pdfelement|phantompdf",
    re.I,
)
GENERIC_AUTHORS: Final = frozenset(
    {"admin", "administrator", "user", "owner", "test", "default", "root", "guest"}
)
_PDF_DATE: Final = re.compile(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?")
_DIGIT_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.-]*\d[A-Za-z0-9/_.-]*")
OVERPRINT_MIN: Final = 4
WHITEOUT_MIN: Final = 2


def numbering(number: str | None) -> str | None:
    """The shape of an invoice number: digits as 9, the rest kept (``INV-9999-99999``)."""
    if not number:
        return None
    return re.sub(r"\d", "9", number.strip())[:64]


def _creator(meta: Mapping[str, str]) -> str:
    return (meta.get("pdf:Creator") or meta.get("docx:app:Application") or "").strip()[:80]


def fingerprint(document: DocumentFacts, invoice_number: str | None) -> dict[str, str]:
    """What a vendor's template looks like, from the parsed document."""
    return {
        "media": document.media_type,
        "fonts": "|".join(document.fonts),
        "accents": "|".join(document.accents),
        "images": "logo" if document.image_count else "none",
        "numbering": numbering(invoice_number) or "",
        "creator": _creator(document.metadata),
        "page_size": document.page_size or "",
    }


def _template(fp: Mapping[str, str], history: History) -> Finding | None:
    past = [
        p.fingerprint
        for p in history.vendor_invoices
        if p.fingerprint and p.fingerprint.get("media") == fp.get("media")
    ]
    if len(past) < MIN_TEMPLATES:
        return None
    differs: list[str] = []
    for feature in FEATURES:
        values = Counter(p.get(feature, "") for p in past)
        usual, count = values.most_common(1)[0]
        if count * 3 >= len(past) * 2 and fp.get(feature, "") != usual:
            differs.append(feature)
    if len(differs) < 2:
        return None
    return finding(
        CHECK,
        "template_mismatch",
        "template_mismatch",
        Severity.HIGH if len(differs) >= 3 else Severity.MEDIUM,
        evidence={"features": ",".join(differs), "compared_with": len(past)},
        features=terms(differs),
    )


def _date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    match = _PDF_DATE.search(value)
    if match:
        parts = [int(p) if p else 0 for p in match.groups()]
        try:
            return dt.datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
        except ValueError:
            return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _metadata(facts: InvoiceFacts, settings: CheckSettings) -> list[Finding]:
    document = facts.document
    if document is None:
        return []
    meta = document.metadata
    out: list[Finding] = []

    def add(rule: str, evidence: dict[str, str | int | bool | None], **values: Any) -> None:
        out.append(
            finding(
                CHECK, "metadata_inconsistency", rule, Severity.MEDIUM, evidence=evidence, **values
            )
        )

    created = _date(meta.get("pdf:CreationDate") or meta.get("docx:created"))
    modified = _date(meta.get("pdf:ModDate") or meta.get("docx:modified"))
    if created and modified and created - modified > dt.timedelta(minutes=1):
        add("metadata_created_after_modified", {"created": created.isoformat()})
    for key in ("pdf:Creator", "pdf:Producer", "docx:app:Application"):
        tool = (meta.get(key) or "").strip()
        if tool and GENERIC_TOOLS.search(tool):
            add("metadata_generic_tool", {"field": key, "tool": tool[:80]}, tool=tool[:80])
            break
    pdf_created = _date(meta.get("pdf:CreationDate"))
    if pdf_created and facts.invoice_date:
        late = (pdf_created.date() - facts.invoice_date).days
        if late > settings.metadata_late_days:
            add("metadata_created_late", {"days": late}, days=late)
    title = meta.get("pdf:Title") or meta.get("docx:title") or ""
    own = number_key(facts.invoice_number)
    if title and own:
        tokens = [number_key(t) for t in _DIGIT_TOKEN.findall(title)]
        keys = [t for t in tokens if t]
        if keys and own not in keys and not any(own in k or k in own for k in keys):
            add("metadata_title_number", {"title_numbers": len(keys)})
    author = (meta.get("pdf:Author") or meta.get("docx:author") or "").strip()
    if author.casefold() in GENERIC_AUTHORS:
        add("metadata_author_generic", {"author": author[:40]}, author=author[:40])
    return out


def _editing(facts: InvoiceFacts) -> list[Finding]:
    out: list[Finding] = []
    document = facts.document
    if document is not None and document.overprinted >= OVERPRINT_MIN:
        out.append(
            finding(
                CHECK,
                "editing_traces",
                "editing_overprint",
                Severity.HIGH,
                evidence={"characters": document.overprinted},
            )
        )
    covered = 0
    report = facts.guardrail_report
    if report is not None:
        for f in report.of_type(FindingType.HIDDEN_TEXT):
            digits = sum(ch.isdigit() for ch in f.evidence)
            if f.code == "covered_or_clipped" and digits and digits * 2 >= len(f.evidence.strip()):
                covered += 1
    whiteout = document.whiteout_digits if document is not None else 0
    if whiteout >= WHITEOUT_MIN or covered:
        out.append(
            finding(
                CHECK,
                "editing_traces",
                "editing_whiteout",
                Severity.MEDIUM,
                evidence={"digits": whiteout, "covered_figures": covered},
            )
        )
    return out


def _trn(facts: InvoiceFacts) -> list[Finding]:
    printed = re.sub(r"[\s-]", "", facts.vendor_trn or "")
    if not printed:
        return []
    if not (len(printed) == 15 and printed.isdigit()):
        return [
            finding(
                CHECK,
                "trn_invalid",
                "trn_invalid",
                Severity.MEDIUM,
                evidence={"length": len(printed)},
                trn=printed[:20],
            )
        ]
    vendor = facts.vendor
    if vendor is not None and vendor.trn and vendor.trn != printed:
        return [
            finding(
                CHECK,
                "trn_mismatch",
                "trn_mismatch",
                Severity.HIGH,
                evidence={"vendor_id": vendor.id, "printed_last3": printed[-3:]},
            )
        ]
    return []


def check_forgery(
    facts: InvoiceFacts,
    history: History,
    settings: CheckSettings,
    *,
    own_fingerprint: Mapping[str, str] | None,
) -> CheckResult:
    findings: list[Finding] = []
    if own_fingerprint is not None:
        template = _template(own_fingerprint, history)
        if template is not None:
            findings.append(template)
    findings += _metadata(facts, settings)
    findings += _editing(facts)
    findings += _trn(facts)
    return result(CHECK, VERSION, findings)

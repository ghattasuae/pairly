# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision receipts (brief 2.3.5): a role-redacted summary as JSON and as a PDF.

The redaction policy is data (``RECEIPT_POLICY``): for every receipt section, and for
every key inside the ``evidence`` and ``personal_data`` sections, it says what each
audience gets: ``show``, ``redact`` (the key stays, the value becomes
``{"redacted": true}``) or ``omit`` (the key is removed). Keys the policy does not
name fall back to the section default, which never shows anything new to a subject.

Audiences:

* ``internal_full``: staff with ``core.decision_personal.read`` (admin by default).
  Everything, decrypted personal data included.
* ``internal``: staff with ``core.decision.read`` (auditors and others). Everything
  except decrypted personal data and internal notes, which are redacted.
* ``subject``: the candidate or vendor the decision is about (portals, M2 and M4).
  Only their own decision (``subject_receipt`` checks it), with internal notes, people,
  model internals, fraud signals and links removed.

The PDF is plain: a title, then one line per value, in English or Arabic. Arabic is
shaped and ordered right to left (``pairly_core.decisions.bidi``) and drawn with the
bundled DejaVu Sans subset, so the PDF needs no system fonts.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pairly_core.audit import format_timestamp
from pairly_core.decisions.models import DecisionRecord
from pairly_core.decisions.permissions import DECISION_PERSONAL_READ, DECISION_READ
from pairly_core.errors import NotFoundError
from pairly_core.rbac import Actor

RECEIPT_FORMAT: Final = "pairly.decision_receipt.v1"
REDACTED: Final = {"redacted": True}
FONT_PATH: Final = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
FONT_NAME: Final = "PairlyDejaVuSans"


class Audience(StrEnum):
    INTERNAL_FULL = "internal_full"
    INTERNAL = "internal"
    SUBJECT = "subject"


class Visibility(StrEnum):
    SHOW = "show"
    REDACT = "redact"
    OMIT = "omit"


_S, _R, _O = Visibility.SHOW, Visibility.REDACT, Visibility.OMIT


def _rule(
    full: Visibility, internal: Visibility, subject: Visibility
) -> dict[Audience, Visibility]:
    return {Audience.INTERNAL_FULL: full, Audience.INTERNAL: internal, Audience.SUBJECT: subject}


# The redaction policy. Paths: a receipt section, or "<section>.<key>" inside the
# ``evidence`` and ``personal_data`` sections; "<section>.*" is that section's default.
RECEIPT_POLICY: Final[Mapping[str, Mapping[Audience, Visibility]]] = {
    "format": _rule(_S, _S, _S),
    "audience": _rule(_S, _S, _S),
    "decision_id": _rule(_S, _S, _S),
    "kind": _rule(_S, _S, _S),
    "workflow": _rule(_S, _S, _S),
    "subject": _rule(_S, _S, _S),
    "decided_at": _rule(_S, _S, _S),
    "recorded_at": _rule(_S, _S, _S),
    "outcome": _rule(_S, _S, _S),
    "confidence": _rule(_S, _S, _S),
    "policy_versions": _rule(_S, _S, _S),
    "prompt_versions": _rule(_S, _S, _O),
    "model": _rule(_S, _S, _O),
    "model_call_ids": _rule(_S, _S, _O),
    "inputs": _rule(_S, _S, _O),
    "human_action": _rule(_S, _S, _S),
    "actor": _rule(_S, _S, _O),
    "internal_note": _rule(_S, _R, _O),
    "links": _rule(_S, _S, _O),
    "integrity": _rule(_S, _S, _S),
    # Evidence: what was seen and which rules applied. Fraud and injection signals stay
    # internal, so a subject cannot learn how to avoid them.
    "evidence.*": _rule(_S, _S, _O),
    "evidence.rule_results": _rule(_S, _S, _S),
    "evidence.quote_locations": _rule(_S, _S, _S),
    "evidence.citations": _rule(_S, _S, _S),
    "evidence.risk_signals": _rule(_S, _S, _O),
    "evidence.guardrail_findings": _rule(_S, _S, _O),
    # Personal data: decrypted only for internal_full, and for the subject (their own).
    "personal_data.*": _rule(_S, _R, _O),
    "personal_data.fields": _rule(_S, _R, _S),
    "personal_data.raw": _rule(_S, _R, _S),
    "personal_data.quotes": _rule(_S, _R, _S),
    "personal_data.facts": _rule(_S, _R, _S),
    "personal_data.reasons": _rule(_S, _R, _S),
    "personal_data.old_value": _rule(_S, _R, _S),
    "personal_data.new_value": _rule(_S, _R, _S),
    "personal_data.findings": _rule(_S, _R, _S),
    "personal_data.question": _rule(_S, _R, _O),
    "personal_data.guardrail_evidence": _rule(_S, _R, _O),
    "personal_data.result": _rule(_S, _R, _O),
}
_NESTED: Final = ("evidence", "personal_data")


def visibility(path: str, audience: Audience) -> Visibility:
    rule = RECEIPT_POLICY.get(path)
    if rule is None and "." in path:
        rule = RECEIPT_POLICY.get(path.split(".", 1)[0] + ".*")
    # Unknown sections are omitted for everyone: a new field is never shown by accident.
    return rule[audience] if rule is not None else Visibility.OMIT


def audience_for(actor: Actor) -> Audience:
    """The staff audience of an actor (the caller has checked ``core.decision.read``)."""
    if actor.has(DECISION_PERSONAL_READ):
        return Audience.INTERNAL_FULL
    if actor.has(DECISION_READ):
        return Audience.INTERNAL
    raise NotFoundError("decision not found")


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """Facts about the record beyond the row itself."""

    sensitive: Mapping[str, Any] | None
    intact: bool
    history: tuple[str, ...] = ()
    superseded_by: str | None = None


def full_receipt(row: DecisionRecord, ctx: ReceiptContext) -> dict[str, Any]:
    """Every section, before redaction."""
    sensitive = dict(ctx.sensitive or {})
    note = sensitive.pop("reason", None)
    return {
        "format": RECEIPT_FORMAT,
        "decision_id": row.id,
        "kind": row.kind,
        "workflow": row.workflow,
        "subject": {"type": row.entity_type, "id": row.entity_id},
        "decided_at": format_timestamp(row.decided_at),
        "recorded_at": format_timestamp(row.created_at) if row.created_at else None,
        "outcome": dict(row.output),
        "confidence": row.confidence,
        "policy_versions": list(row.policy_versions),
        "prompt_versions": list(row.prompt_versions),
        "model": dict(row.model),
        "model_call_ids": [str(c) for c in row.model_call_ids],
        "inputs": dict(row.inputs),
        "evidence": dict(row.evidence),
        "personal_data": sensitive,
        "human_action": {"action": row.final_action} if row.final_action else None,
        "actor": {"type": row.actor_type, "id": row.actor_id},
        "internal_note": note,
        "links": {
            "supersedes": row.supersedes,
            "superseded_by": ctx.superseded_by,
            "history": list(ctx.history),
            "audit_event_ids": [str(a) for a in row.audit_event_ids],
            "approval_ids": [str(a) for a in row.approval_ids],
        },
        "integrity": {"content_hash": row.content_hash, "intact": ctx.intact},
    }


def redact(receipt: Mapping[str, Any], audience: Audience) -> dict[str, Any]:
    """Apply ``RECEIPT_POLICY`` for ``audience``. Pure; the input is not changed."""
    out: dict[str, Any] = {}
    redacted: list[str] = []
    for key, value in receipt.items():
        if key in _NESTED and isinstance(value, Mapping):
            section: dict[str, Any] = {}
            for sub, item in value.items():
                path = f"{key}.{sub}"
                rule = visibility(path, audience)
                if rule is Visibility.SHOW:
                    section[sub] = item
                elif rule is Visibility.REDACT:
                    section[sub] = dict(REDACTED)
                    redacted.append(path)
            out[key] = section
            continue
        rule = visibility(key, audience)
        if value is None and rule is not Visibility.OMIT:
            out[key] = None
        elif rule is Visibility.SHOW:
            out[key] = value
        elif rule is Visibility.REDACT:
            out[key] = dict(REDACTED)
            redacted.append(key)
    out["audience"] = audience.value
    if audience is not Audience.SUBJECT:
        out["redacted"] = sorted(redacted)
    return out


def receipt(row: DecisionRecord, ctx: ReceiptContext, audience: Audience) -> dict[str, Any]:
    return redact(full_receipt(row, ctx), audience)


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """A candidate or vendor, as the decision names them (``entity_type``, ``entity_id``)."""

    type: str
    id: str


def subject_receipt(
    row: DecisionRecord, ctx: ReceiptContext, subject: SubjectRef
) -> dict[str, Any]:
    """A subject's receipt. Another subject's decision reads as not found."""
    if (row.entity_type, row.entity_id) != (subject.type, subject.id):
        raise NotFoundError("decision not found")
    return receipt(row, ctx, Audience.SUBJECT)


# ------------------------------------------------------------------ PDF

LABELS: Final[Mapping[str, Mapping[str, str]]] = {
    "title": {"en": "Decision receipt", "ar": "إيصال القرار"},
    "decision_id": {"en": "Decision", "ar": "القرار"},
    "kind": {"en": "Kind", "ar": "النوع"},
    "workflow": {"en": "Workflow", "ar": "سير العمل"},
    "subject": {"en": "About", "ar": "بخصوص"},
    "decided_at": {"en": "Decided at", "ar": "تاريخ القرار"},
    "recorded_at": {"en": "Recorded at", "ar": "تاريخ التسجيل"},
    "outcome": {"en": "Outcome", "ar": "النتيجة"},
    "confidence": {"en": "Confidence", "ar": "درجة الثقة"},
    "policy_versions": {"en": "Policy versions", "ar": "إصدارات السياسة"},
    "prompt_versions": {"en": "Prompt versions", "ar": "إصدارات التعليمات"},
    "model": {"en": "Model", "ar": "النموذج"},
    "model_call_ids": {"en": "Model calls", "ar": "استدعاءات النموذج"},
    "inputs": {"en": "Inputs", "ar": "المدخلات"},
    "evidence": {"en": "Evidence", "ar": "الأدلة"},
    "personal_data": {"en": "Personal data", "ar": "البيانات الشخصية"},
    "human_action": {"en": "Human action", "ar": "الإجراء البشري"},
    "actor": {"en": "Taken by", "ar": "اتخذه"},
    "internal_note": {"en": "Internal note", "ar": "ملاحظة داخلية"},
    "links": {"en": "Links", "ar": "الروابط"},
    "integrity": {"en": "Integrity", "ar": "السلامة"},
    "redacted": {"en": "Redacted", "ar": "محجوب"},
    "audience": {"en": "Audience", "ar": "الجمهور"},
    "generated": {"en": "Generated", "ar": "أنشئ في"},
    "redacted_value": {"en": "[redacted]", "ar": "[محجوب]"},
    "none": {"en": "none", "ar": "لا يوجد"},
}
_SKIP_IN_PDF = frozenset({"format"})
MAX_PDF_LINES: Final = 2000


def _label(key: str, lang: str) -> str:
    return LABELS.get(key, {}).get(lang) or key.replace("_", " ")


def _flatten(value: Any, lang: str, prefix: str = "", depth: int = 0) -> Iterator[str]:
    """One line per scalar, ``path: value``."""
    if value == REDACTED:
        yield f"{prefix}{_label('redacted_value', lang)}"
        return
    if depth > 6:
        yield f"{prefix}..."
        return
    if isinstance(value, Mapping):
        if not value:
            yield f"{prefix}{_label('none', lang)}"
        for key, item in value.items():
            if isinstance(item, Mapping | list) and item and item != REDACTED:
                yield f"{prefix}{key}:"
                yield from _flatten(item, lang, prefix + "  ", depth + 1)
            else:
                yield from _flatten(item, lang, f"{prefix}{key}: ", depth + 1)
        return
    if isinstance(value, list):
        if not value:
            yield f"{prefix}{_label('none', lang)}"
        for item in value:
            yield from _flatten(item, lang, prefix + "- ", depth + 1)
        return
    if value is None:
        yield f"{prefix}{_label('none', lang)}"
    elif isinstance(value, bool):
        yield f"{prefix}{'true' if value else 'false'}"
    else:
        yield f"{prefix}{value}"


def receipt_lines(data: Mapping[str, Any], lang: str) -> list[tuple[str, bool]]:
    """``(text, is_heading)`` lines of a redacted receipt, in reading order."""
    lines: list[tuple[str, bool]] = []
    for key, value in data.items():
        if key in _SKIP_IN_PDF:
            continue
        if isinstance(value, Mapping | list) and value and value != REDACTED:
            lines.append((_label(key, lang), True))
            lines.extend((line, False) for line in _flatten(value, lang, "  "))
        else:
            text = next(iter(_flatten(value, lang)), "")
            lines.append((f"{_label(key, lang)}: {text}", False))
    return lines[:MAX_PDF_LINES]


def _font() -> str:
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
    return FONT_NAME


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if stringWidth(candidate, font, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        while stringWidth(current, font, size) > width and len(current) > 1:
            cut = len(current)
            while cut > 1 and stringWidth(current[:cut], font, size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    lines.append(current)
    return lines


def render_pdf(data: Mapping[str, Any], *, lang: str = "en") -> bytes:
    """A plain A4 PDF of a redacted receipt (``lang``: ``en`` or ``ar``)."""
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

    from pairly_core.decisions.bidi import has_rtl, shape

    if lang not in {"en", "ar"}:
        raise ValueError("lang must be en or ar")
    rtl = lang == "ar"
    font = _font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=True, pageCompression=1)
    pdf.setTitle(f"{LABELS['title']['en']} {data.get('decision_id', '')}")
    pdf.setAuthor("Pairly")
    width, height = A4
    margin = 50.0
    usable = width - 2 * margin
    y = height - margin

    def draw(text: str, size: float) -> None:
        nonlocal y
        for part in _wrap(text, font, size, usable):
            if y < margin:
                pdf.showPage()
                y = height - margin
            pdf.setFont(font, size)
            if rtl:
                pdf.drawRightString(width - margin, y, shape(part, rtl=True))
            else:
                pdf.drawString(margin, y, shape(part, rtl=False) if has_rtl(part) else part)
            y -= size * 1.45

    draw(LABELS["title"][lang], 16)
    y -= 6
    stamp = format_timestamp(datetime.now(UTC))
    draw(f"{_label('generated', lang)}: {stamp}", 8)
    y -= 4
    for text, heading in receipt_lines(data, lang):
        if heading:
            y -= 4
        draw(text, 11 if heading else 9)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""GL coding and cost centre suggestions (brief 4.5.3), learned from history and
corrections, with confidence.

**Lists.** Each organisation keeps a chart of accounts and a list of cost centres
(``pay_coding_lists``, ``pay_coding_entries``), created from JSON or imported from CSV
(``code,name,name_ar``). A list is never edited: every change is a new version and the
newest version is in force. A coded line records the versions its codes came from.
Validation is strict and all or nothing (the CSV rules of ``pairly_pay.purchase_orders``:
known columns, no formulas, every problem with its row).

**Suggestions, deterministic first.** For each line of an invoice, the lines coded
before (``pay_invoice_line_codings``, this organisation only, newest ``MAX_HISTORY``)
vote for their GL account and cost centre:

* similarity of the descriptions: 1 for the same words in any order of case and
  punctuation, else the share of words they have in common (Jaccard);
* the same vendor counts in full, another vendor ``OTHER_VENDOR`` of it;
* a line a person coded counts 1, a line where a person corrected the suggestion counts
  ``CORRECTED_WEIGHT`` (corrections feed back), a line the system coded ``AUTO_WEIGHT``;
* lines under ``MIN_SIMILARITY`` do not vote, nor codes no longer in the list in force.

Per field, the confidence is ``agreement * best similarity * support`` where agreement is
the winning code's share of the votes and support grows with the number of lines that
back it (one line: 2/3, two or more: 1). ``high`` from 0.85, ``medium`` from 0.6, else
``low``. A suggestion is only a suggestion: a person confirms it, unless the organisation
turned on ``auto_code_high_confidence`` (off by default), in which case the intake codes
uncoded lines whose suggestion is ``high`` (source ``auto``, audited).

**Model suggestion, optional.** ``model_suggestion`` asks the model router
(``pay_coding_suggest/v1``) for a line the history cannot code. The model sees the line
description and the codes in force, nothing else. Its answer must name codes of the
lists in force or it is dropped. It is never applied by itself, whatever the settings.

**Coding.** ``set_line_codings`` stores a person's codes (validated against the lists
in force). When they differ from the suggestion of the moment, a correction is filed
(``pay_coding_corrections``) and the line weighs more in later suggestions. The coding
is part of what the checks read (``pairly_pay.freshness``): the caller runs them again.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.errors import NotFoundError
from pairly_core.extraction.service import load_extraction
from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_core.rbac import Actor
from pairly_pay.checks.types import Line
from pairly_pay.common import By, audit, clean_text, record_step
from pairly_pay.invoices import INVOICE_ENTITY, InvoiceStateError, get_invoice
from pairly_pay.models import (
    CodingKind,
    CodingSource,
    InvoiceStatus,
    PayCodingCorrection,
    PayCodingEntry,
    PayCodingList,
    PayInvoice,
    PayInvoiceLineCoding,
    RecordSource,
)
from pairly_pay.purchase_orders import InvalidPurchaseDataError, RowError, read_csv

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

LIST_ENTITY: Final = "pay.coding_list"
MAX_ENTRIES: Final = 2000
MAX_HISTORY: Final = 2000
MAX_EXAMPLES: Final = 3
MIN_SIMILARITY: Final = 0.5
OTHER_VENDOR: Final = 0.7
CORRECTED_WEIGHT: Final = 1.5
AUTO_WEIGHT: Final = 0.6
HIGH: Final = 0.85
MEDIUM: Final = 0.6
# The model sees at most this many codes of each list (a bigger chart: no model call).
MAX_MODEL_CODES: Final = 300
COLUMNS: Final = ("code", "name", "name_ar")
REQUIRED: Final = frozenset({"code", "name"})
CODE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_WORDS: Final = re.compile(r"\w+", re.UNICODE)
_FORMULA: Final = ("=", "+", "@", "\t", "\r")
CODABLE: Final = frozenset({InvoiceStatus.NEEDS_REVIEW.value, InvoiceStatus.READY.value})
_LINE: Final = re.compile(r"^line_items\[(\d{1,3})\]\.([a-z_]+)$")


# ------------------------------------------------------------------ lists


@dataclass(frozen=True, slots=True)
class EntryInput:
    code: str
    name: str
    name_ar: str | None = None


def _text_problem(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if value.startswith(_FORMULA):
        return "formula_not_allowed"
    if len(value) > limit:
        return "too_long"
    return None


def validate_entries(entries: Sequence[EntryInput], rows: Sequence[int] = ()) -> list[RowError]:
    """Every problem of a list (``rows``: the CSV row of each entry; else row 0)."""
    errors: list[RowError] = []
    if not entries:
        errors.append(RowError(0, "entries", "required"))
    if len(entries) > MAX_ENTRIES:
        errors.append(RowError(0, "entries", "too_many"))
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        row = rows[i] if i < len(rows) else 0
        code = entry.code.strip()
        if not CODE.fullmatch(code):
            errors.append(RowError(row, "code", "invalid_code"))
        elif code.upper() in seen:
            errors.append(RowError(row, "code", "duplicate_code"))
        seen.add(code.upper())
        if not entry.name.strip():
            errors.append(RowError(row, "name", "required"))
        for name, value in (("name", entry.name), ("name_ar", entry.name_ar)):
            problem = _text_problem(value, 160)
            if problem:
                errors.append(RowError(row, name, problem))
    return errors


async def current_list(session: AsyncSession, kind: CodingKind) -> PayCodingList | None:
    """The version in force (the newest), or None when there is none."""
    return await session.scalar(
        select(PayCodingList)
        .where(PayCodingList.kind == kind.value)
        .order_by(PayCodingList.version.desc())
        .limit(1)
    )


async def list_versions(session: AsyncSession, kind: CodingKind) -> list[PayCodingList]:
    rows = await session.scalars(
        select(PayCodingList)
        .where(PayCodingList.kind == kind.value)
        .order_by(PayCodingList.version.desc())
        .limit(200)
    )
    return list(rows)


async def entries_of(
    session: AsyncSession, coding_list: PayCodingList | None
) -> list[PayCodingEntry]:
    if coding_list is None:
        return []
    rows = await session.scalars(
        select(PayCodingEntry).where(PayCodingEntry.list_id == coding_list.id)
    )
    # Sorted here, not by the database collation: the model sees them in this order.
    return sorted(rows, key=lambda e: e.code)


@dataclass(frozen=True, slots=True)
class Lists:
    """The lists in force and their codes."""

    gl: PayCodingList | None
    cc: PayCodingList | None
    gl_entries: tuple[PayCodingEntry, ...] = ()
    cc_entries: tuple[PayCodingEntry, ...] = ()

    @property
    def gl_codes(self) -> dict[str, PayCodingEntry]:
        return {e.code: e for e in self.gl_entries}

    @property
    def cc_codes(self) -> dict[str, PayCodingEntry]:
        return {e.code: e for e in self.cc_entries}


async def lists_in_force(session: AsyncSession) -> Lists:
    gl = await current_list(session, CodingKind.GL_ACCOUNT)
    cc = await current_list(session, CodingKind.COST_CENTRE)
    return Lists(gl, cc, tuple(await entries_of(session, gl)), tuple(await entries_of(session, cc)))


async def create_list(
    session: AsyncSession,
    by: By,
    kind: CodingKind,
    entries: Sequence[EntryInput],
    *,
    source: RecordSource = RecordSource.API,
    rows: Sequence[int] = (),
) -> PayCodingList:
    """A new version of the list (validated, all or nothing, audited)."""
    errors = validate_entries(entries, rows)
    if errors:
        raise InvalidPurchaseDataError(errors)
    # One version at a time per list kind (the unique version is the last line of defence).
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"pay.coding:{kind.value}"}
    )
    latest = await session.scalar(
        select(func.max(PayCodingList.version)).where(PayCodingList.kind == kind.value)
    )
    coding_list = PayCodingList(
        id=uuid.uuid4(),
        kind=kind.value,
        version=int(latest or 0) + 1,
        entry_count=len(entries),
        source=source.value,
        created_by_type=by.actor_type.value,
        created_by_id=by.actor_id,
    )
    session.add(coding_list)
    await session.flush()
    for entry in sorted(entries, key=lambda e: e.code.strip()):
        session.add(
            PayCodingEntry(
                list_id=coding_list.id,
                kind=kind.value,
                code=entry.code.strip(),
                name=clean_text(entry.name, 160),
                name_ar=clean_text(entry.name_ar, 160) or None,
            )
        )
    await session.flush()
    await audit(
        session,
        by,
        "pay.coding_list.created",
        entity_type=LIST_ENTITY,
        entity_id=coding_list.id,
        payload={
            "kind": kind.value,
            "version": coding_list.version,
            "entries": coding_list.entry_count,
            "source": source.value,
        },
    )
    return coding_list


async def import_list(
    session: AsyncSession, by: By, kind: CodingKind, content: str | bytes
) -> PayCodingList:
    """A new version from CSV (``code,name,name_ar``, one row per code). All or nothing."""
    parsed = read_csv(content, COLUMNS, REQUIRED)
    entries = [
        EntryInput(row.get("code", ""), row.get("name", ""), row.get("name_ar") or None)
        for _, row in parsed.rows
    ]
    rows = [n for n, _ in parsed.rows]
    if parsed.errors:
        # Every problem at once: the format ones and those of the values that were read.
        found = list(parsed.errors) + (validate_entries(entries, rows) if entries else [])
        unique = {(e.row, e.field, e.code): e for e in found}
        raise InvalidPurchaseDataError(
            sorted(unique.values(), key=lambda e: (e.row, e.field or ""))
        )
    return await create_list(session, by, kind, entries, source=RecordSource.CSV, rows=rows)


# ------------------------------------------------------------------ suggestions


def words(description: str | None) -> frozenset[str]:
    return frozenset(w for w in _WORDS.findall((description or "").casefold()) if w)


def similarity(a: str | None, b: str | None) -> float:
    left, right = words(a), words(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class PastCoding:
    invoice_id: uuid.UUID
    reference: str
    vendor_id: uuid.UUID | None
    description: str
    gl_code: str | None
    cost_centre: str | None
    source: str
    corrected: bool

    @property
    def weight(self) -> float:
        if self.source == CodingSource.AUTO.value:
            return AUTO_WEIGHT
        return CORRECTED_WEIGHT if self.corrected else 1.0


@dataclass(frozen=True, slots=True)
class FieldGuess:
    code: str
    confidence: float
    support: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    gl_code: str | None
    cost_centre: str | None
    confidence: float
    method: str  # "history" or "model"
    support: int = 0
    examples: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def level(self) -> str:
        return level_of(self.confidence)

    def as_json(self) -> dict[str, Any]:
        return {
            "gl_code": self.gl_code,
            "cost_centre": self.cost_centre,
            # A string: audit payloads and decision records never hold floats.
            "confidence": f"{self.confidence:.3f}",
            "level": self.level,
            "method": self.method,
            "support": self.support,
        }


def level_of(confidence: float) -> str:
    if confidence >= HIGH:
        return "high"
    if confidence >= MEDIUM:
        return "medium"
    return "low"


def _guess(votes: Mapping[str, list[tuple[float, float, str]]]) -> FieldGuess | None:
    """The winning code of one field. ``votes[code]``: (similarity, weight, reference)."""
    if not votes:
        return None
    scores = {code: sum(s * w for s, w, _ in items) for code, items in votes.items()}
    total = sum(scores.values())
    if total <= 0:
        return None
    best = sorted(scores, key=lambda c: (-scores[c], c))[0]
    backers = votes[best]
    agreement = scores[best] / total
    best_similarity = max(s for s, _, _ in backers)
    support = min(1.0, (len(backers) + 1) / 3)
    return FieldGuess(best, agreement * best_similarity * support, len(backers))


def suggest_from_history(
    description: str | None,
    vendor_id: uuid.UUID | None,
    history: Sequence[PastCoding],
    gl_codes: frozenset[str] | set[str],
    cc_codes: frozenset[str] | set[str],
) -> Suggestion | None:
    """The deterministic suggestion for one line (see the module docstring), or None."""
    if not words(description):
        return None
    gl_votes: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    cc_votes: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for past in history:
        score = similarity(description, past.description)
        if vendor_id is None or past.vendor_id != vendor_id:
            score *= OTHER_VENDOR
        if score < MIN_SIMILARITY:
            continue
        if past.gl_code and past.gl_code in gl_codes:
            gl_votes[past.gl_code].append((score, past.weight, past.reference))
        if past.cost_centre and past.cost_centre in cc_codes:
            cc_votes[past.cost_centre].append((score, past.weight, past.reference))
    gl = _guess(gl_votes)
    cc = _guess(cc_votes)
    if gl is None and cc is None:
        return None
    parts = [g.confidence for g in (gl, cc) if g is not None]
    examples: list[str] = []
    for guess, votes in ((gl, gl_votes), (cc, cc_votes)):
        if guess is None:
            continue
        for _, _, reference in sorted(votes[guess.code], key=lambda v: -v[0]):
            if reference not in examples:
                examples.append(reference)
    return Suggestion(
        gl_code=gl.code if gl else None,
        cost_centre=cc.code if cc else None,
        confidence=min(parts),
        method="history",
        support=max(g.support for g in (gl, cc) if g is not None),
        examples=tuple(examples[:MAX_EXAMPLES]),
    )


async def coding_history(session: AsyncSession, exclude: uuid.UUID) -> list[PastCoding]:
    """Lines coded before, newest first (this organisation only: the session is org bound)."""
    rows = await session.execute(
        select(PayInvoiceLineCoding, PayInvoice.reference)
        .join(PayInvoice, PayInvoice.id == PayInvoiceLineCoding.invoice_id)
        .where(
            PayInvoiceLineCoding.invoice_id != exclude,
            (PayInvoiceLineCoding.gl_code.is_not(None))
            | (PayInvoiceLineCoding.cost_centre.is_not(None)),
        )
        .order_by(PayInvoiceLineCoding.coded_at.desc(), PayInvoiceLineCoding.id)
        .limit(MAX_HISTORY)
    )
    return [
        PastCoding(
            invoice_id=row.invoice_id,
            reference=reference,
            vendor_id=row.vendor_id,
            description=row.description,
            gl_code=row.gl_code,
            cost_centre=row.cost_centre,
            source=row.source,
            corrected=row.corrected,
        )
        for row, reference in rows.all()
    ]


def lines_from_values(values: Mapping[str, Any]) -> tuple[Line, ...]:
    """The line items of the current values (index and description are what coding needs)."""
    rows: dict[int, dict[str, Any]] = {}
    for path, value in values.items():
        match = _LINE.match(path)
        if match:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = value
    out: list[Line] = []
    for index, row in sorted(rows.items()):
        raw = row.get("description")
        text_value = raw.strip() if isinstance(raw, str) and raw.strip() else None
        out.append(Line(index=index, description=text_value))
    return tuple(out)


async def invoice_lines(session: AsyncSession, invoice: PayInvoice) -> tuple[Line, ...]:
    stored = (
        await load_extraction(session, invoice.extraction_id) if invoice.extraction_id else None
    )
    return lines_from_values(stored.current_values()) if stored is not None else ()


async def line_codings(
    session: AsyncSession, invoice_id: uuid.UUID, *, lock: bool = False
) -> dict[int, PayInvoiceLineCoding]:
    query = select(PayInvoiceLineCoding).where(PayInvoiceLineCoding.invoice_id == invoice_id)
    if lock:
        query = query.with_for_update()
    return {row.line_index: row for row in await session.scalars(query)}


@dataclass(frozen=True, slots=True)
class LineView:
    line: Line
    coding: PayInvoiceLineCoding | None
    suggestion: Suggestion | None


@dataclass(frozen=True, slots=True)
class CodingView:
    lists: Lists
    lines: tuple[LineView, ...]
    auto_coding: bool = False


async def suggestions_for(
    session: AsyncSession, invoice: PayInvoice, lines: Sequence[Line], lists: Lists
) -> dict[int, Suggestion]:
    history = await coding_history(session, invoice.id)
    gl_codes = set(lists.gl_codes)
    cc_codes = set(lists.cc_codes)
    out: dict[int, Suggestion] = {}
    for line in lines:
        found = suggest_from_history(
            line.description, invoice.vendor_id, history, gl_codes, cc_codes
        )
        if found is not None:
            out[line.index] = found
    return out


async def coding_view(
    session: AsyncSession, invoice: PayInvoice, *, auto_coding: bool = False
) -> CodingView:
    lists = await lists_in_force(session)
    lines = await invoice_lines(session, invoice)
    current = await line_codings(session, invoice.id)
    suggested = await suggestions_for(session, invoice, lines, lists)
    return CodingView(
        lists=lists,
        lines=tuple(
            LineView(line, current.get(line.index), suggested.get(line.index)) for line in lines
        ),
        auto_coding=auto_coding,
    )


# ------------------------------------------------------------------ coding


@dataclass(frozen=True, slots=True)
class LineCodingInput:
    line_index: int
    gl_code: str | None
    cost_centre: str | None


@dataclass
class CodingResult:
    invoice: PayInvoice
    changed: list[int] = field(default_factory=list)
    corrected: list[int] = field(default_factory=list)


def _differs(suggested: str | None, chosen: str | None) -> bool:
    return suggested is not None and suggested != chosen


async def set_line_codings(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, items: Sequence[LineCodingInput]
) -> CodingResult:
    """A person codes invoice lines (the caller runs the checks again when anything
    changed). Codes must be in the lists in force; ``None`` leaves a field uncoded."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.status not in CODABLE:
        raise InvoiceStateError(f"the invoice is {invoice.status}")
    lines = {line.index: line for line in await invoice_lines(session, invoice)}
    lists = await lists_in_force(session)
    gl_codes, cc_codes = lists.gl_codes, lists.cc_codes
    errors: list[RowError] = []
    seen: set[int] = set()
    for item in items:
        if item.line_index not in lines:
            errors.append(RowError(0, "line_index", "line_not_found"))
        if item.line_index in seen:
            errors.append(RowError(0, "line_index", "duplicate_line"))
        seen.add(item.line_index)
        if item.gl_code is not None and item.gl_code not in gl_codes:
            errors.append(RowError(0, "gl_code", "code_not_in_list"))
        if item.cost_centre is not None and item.cost_centre not in cc_codes:
            errors.append(RowError(0, "cost_centre", "code_not_in_list"))
    if errors:
        raise InvalidPurchaseDataError(errors)
    current = await line_codings(session, invoice.id, lock=True)
    suggested = await suggestions_for(session, invoice, list(lines.values()), lists)
    result = CodingResult(invoice)
    by = By.of(actor)
    now = dt.datetime.now(dt.UTC)
    for item in sorted(items, key=lambda x: x.line_index):
        row = current.get(item.line_index)
        if (
            row is not None
            and row.gl_code == item.gl_code
            and row.cost_centre == item.cost_centre
            and row.source == CodingSource.PERSON.value
        ):
            continue
        suggestion = suggested.get(item.line_index)
        corrected = suggestion is not None and (
            _differs(suggestion.gl_code, item.gl_code)
            or _differs(suggestion.cost_centre, item.cost_centre)
        )
        line = lines[item.line_index]
        if row is None:
            row = PayInvoiceLineCoding(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                line_index=item.line_index,
                coded_by_type=by.actor_type.value,
                coded_by_id=by.actor_id,
            )
            session.add(row)
            current[item.line_index] = row
        row.vendor_id = invoice.vendor_id
        row.description = clean_text(line.description, 500)
        row.gl_code = item.gl_code
        row.cost_centre = item.cost_centre
        row.gl_list_id = lists.gl.id if item.gl_code and lists.gl else None
        row.cc_list_id = lists.cc.id if item.cost_centre and lists.cc else None
        row.source = CodingSource.PERSON.value
        row.suggestion = suggestion.as_json() if suggestion is not None else {}
        row.corrected = corrected
        row.coded_by_type = by.actor_type.value
        row.coded_by_id = by.actor_id
        row.coded_at = now
        result.changed.append(item.line_index)
        if corrected and suggestion is not None:
            result.corrected.append(item.line_index)
            session.add(
                PayCodingCorrection(
                    invoice_id=invoice.id,
                    line_index=item.line_index,
                    vendor_id=invoice.vendor_id,
                    suggested=suggestion.as_json(),
                    chosen_gl=item.gl_code,
                    chosen_cost_centre=item.cost_centre,
                    corrected_by=actor.id,
                )
            )
    if not result.changed:
        return result
    invoice.updated_at = now
    await session.flush()
    coded = [
        {
            "line": i,
            "gl_code": current[i].gl_code,
            "cost_centre": current[i].cost_centre,
            "corrected": current[i].corrected,
        }
        for i in result.changed
    ]
    await record_step(
        session,
        by,
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.lines_coded",
        inputs={
            "gl_list_version": lists.gl.version if lists.gl else None,
            "cc_list_version": lists.cc.version if lists.cc else None,
            "suggestions": {
                str(i): suggested[i].as_json() for i in result.changed if i in suggested
            },
        },
        output={"lines": coded},
    )
    await audit(
        session,
        actor,
        "pay.invoice.lines_coded",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"lines": coded[:100], "corrected": result.corrected[:100]},
    )
    return result


async def auto_code(session: AsyncSession, invoice: PayInvoice) -> list[int]:
    """Code the uncoded lines whose suggestion is ``high`` (only called when the
    organisation turned ``auto_code_high_confidence`` on). Returns the lines coded."""
    lists = await lists_in_force(session)
    if lists.gl is None:
        return []
    lines = await invoice_lines(session, invoice)
    current = await line_codings(session, invoice.id, lock=True)
    suggested = await suggestions_for(session, invoice, lines, lists)
    by = By.system("pairly.pay.coding")
    coded: list[dict[str, Any]] = []
    for line in lines:
        suggestion = suggested.get(line.index)
        if line.index in current or suggestion is None or suggestion.level != "high":
            continue
        if suggestion.gl_code is None:
            continue
        session.add(
            PayInvoiceLineCoding(
                invoice_id=invoice.id,
                line_index=line.index,
                vendor_id=invoice.vendor_id,
                description=clean_text(line.description, 500),
                gl_code=suggestion.gl_code,
                cost_centre=suggestion.cost_centre,
                gl_list_id=lists.gl.id,
                cc_list_id=lists.cc.id if suggestion.cost_centre and lists.cc else None,
                source=CodingSource.AUTO.value,
                suggestion=suggestion.as_json(),
                corrected=False,
                coded_by_type=by.actor_type.value,
                coded_by_id=by.actor_id,
            )
        )
        coded.append({"line": line.index, **suggestion.as_json()})
    if not coded:
        return []
    await session.flush()
    await audit(
        session,
        by,
        "pay.invoice.auto_coded",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"lines": coded[:100]},
    )
    return [int(c["line"]) for c in coded]


# ------------------------------------------------------------------ model suggestion

TASK: Final = TaskSpec(name="pay_coding_suggest", prompt_version="v1")
WORKFLOW: Final = "pay"
_Code = Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)]
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class ModelCoding(BaseModel):
    """What the model returns: codes of the lists it was shown (empty: none fits)."""

    model_config = ConfigDict(extra="forbid")

    gl_code: _Code
    cost_centre: _Code = ""
    reason: _Reason = ""


class NoModelSuggestionError(NotFoundError):
    """The model may not be asked (no list, too many codes) or gave nothing usable."""

    code = "no_model_suggestion"
    title = "No model suggestion"


def model_inputs(
    description: str, gl_entries: Sequence[PayCodingEntry], cc_entries: Sequence[PayCodingEntry]
) -> dict[str, Any]:
    """What the model sees: the line description and the codes in force. Never the
    vendor, the amounts or anything about people."""
    return {
        "description": description.strip()[:500],
        "gl_accounts": [{"code": e.code, "name": e.name} for e in gl_entries],
        "cost_centres": [{"code": e.code, "name": e.name} for e in cc_entries],
    }


def accept_model_answer(answer: ModelCoding, lists: Lists) -> Suggestion | None:
    """The answer as a suggestion when it names codes in force; else None (dropped)."""
    gl = answer.gl_code or None
    cc = answer.cost_centre or None
    if gl is None or gl not in lists.gl_codes:
        return None
    if cc is not None and cc not in lists.cc_codes:
        cc = None
    return Suggestion(
        gl_code=gl,
        cost_centre=cc,
        confidence=MEDIUM,
        method="model",
        reason=answer.reason or None,
    )


@dataclass(frozen=True, slots=True)
class ModelSuggestion:
    suggestion: Suggestion
    call_id: uuid.UUID
    from_recording: bool


async def model_suggestion(
    router: ModelRouter,
    org_id: uuid.UUID,
    invoice: PayInvoice,
    line: Line,
    lists: Lists,
    *,
    actor_id: uuid.UUID | None,
) -> ModelSuggestion:
    """Ask the model for one line. Raises ``ModelUnavailable`` (no recording, no key:
    fail closed) or ``NoModelSuggestionError``."""
    if not line.description or not lists.gl_entries:
        raise NoModelSuggestionError("nothing to ask: no description or no chart of accounts")
    if len(lists.gl_entries) > MAX_MODEL_CODES or len(lists.cc_entries) > MAX_MODEL_CODES:
        raise NoModelSuggestionError("the lists are too long to show the model")
    result: RouterResult[ModelCoding] = await router.complete(
        task=TASK,
        inputs=model_inputs(line.description, lists.gl_entries, lists.cc_entries),
        schema=ModelCoding,
        org_id=org_id,
        context=CallContext(
            actor_id=str(actor_id) if actor_id else None,
            workflow=WORKFLOW,
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice.id),
        ),
    )
    accepted = accept_model_answer(result.value, lists)
    if accepted is None:
        raise NoModelSuggestionError("the model named no code of the lists in force")
    return ModelSuggestion(accepted, result.call_id, result.from_recording)

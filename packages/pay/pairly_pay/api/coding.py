# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""GL coding routes (brief 4.5.3, ``pairly_pay.coding``).

* ``GET  /coding``                          the chart of accounts and the cost centres in
  force, with their codes (``pay.invoice.read``).
* ``GET  /coding/{list}/versions``          every version of a list (``gl-accounts`` or
  ``cost-centres``).
* ``POST /coding/{list}``                   a new version from JSON (``pay.coding.manage``,
  a second factor; people only).
* ``POST /coding/{list}/import``            a new version from CSV (``code,name,name_ar``).
* ``GET  /invoices/{id}/coding``            each invoice line with its codes and the
  suggestion from history, with confidence (``pay.invoice.read``).
* ``PUT  /invoices/{id}/coding/lines``      a person codes lines (``pay.invoice.process``).
  The checks run again in the same transaction (the coding is part of what they read).
* ``POST /invoices/{id}/coding/model-suggestions``  ask the model for lines the history
  cannot code (``pay.invoice.process``). Shown to the person, never applied by itself.

Invalid input answers 400 ``invalid_purchase_data`` with every problem.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from pairly_api.problems import problem_response
from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_pay.api.invoices import CHECKS_STATE, Runtime, check_services
from pairly_pay.checks.service import RecheckTrigger, load_settings, recheck_invoice
from pairly_pay.coding import (
    CODE,
    MAX_ENTRIES,
    CodingView,
    EntryInput,
    LineCodingInput,
    NoModelSuggestionError,
    Suggestion,
    coding_view,
    create_list,
    entries_of,
    import_list,
    list_versions,
    lists_in_force,
    model_suggestion,
    set_line_codings,
)
from pairly_pay.common import By
from pairly_pay.freshness import checks_problem
from pairly_pay.invoices import get_invoice, refresh_decisions
from pairly_pay.models import CodingKind, PayCodingEntry, PayCodingList
from pairly_pay.permissions import CODING_MANAGE, INVOICE_PROCESS, INVOICE_READ
from pairly_pay.purchase_orders import MAX_CSV_BYTES, InvalidPurchaseDataError

log = structlog.get_logger("pairly_pay.api.coding")

router = APIRouter(tags=["pay"])
LIST_PATHS: Final = {"gl-accounts": CodingKind.GL_ACCOUNT, "cost-centres": CodingKind.COST_CENTRE}
ListPath = Literal["gl-accounts", "cost-centres"]
MAX_MODEL_LINES: Final = 20
CodeText = Annotated[str, Field(pattern=CODE.pattern)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayCodingEntryIn(_In):
    code: CodeText
    name: Annotated[str, Field(min_length=1, max_length=160)]
    name_ar: Annotated[str, Field(max_length=160)] | None = None


class PayCodingListIn(_In):
    entries: Annotated[list[PayCodingEntryIn], Field(min_length=1, max_length=MAX_ENTRIES)]


class PayCodingCsvIn(_In):
    csv: Annotated[str, Field(min_length=1, max_length=MAX_CSV_BYTES)]


class PayLineCodingIn(_In):
    line_index: Annotated[int, Field(ge=0, le=999)]
    gl_code: CodeText | None = None
    cost_centre: CodeText | None = None


class PayInvoiceLinesCodingIn(_In):
    lines: Annotated[list[PayLineCodingIn], Field(min_length=1, max_length=500)]


class PayModelSuggestionsIn(_In):
    # Lines to ask about; empty: every line without a high confidence suggestion.
    line_indexes: Annotated[list[Annotated[int, Field(ge=0, le=999)]], Field(max_length=50)] = []


class PayCodingEntryOut(BaseModel):
    code: str
    name: str
    name_ar: str | None


class PayCodingListOut(BaseModel):
    id: uuid.UUID
    kind: str
    version: int
    entry_count: int
    source: str
    created_by_type: str
    created_at: dt.datetime


class PayCodingListDetail(PayCodingListOut):
    entries: list[PayCodingEntryOut]


class PayCodingListsOut(BaseModel):
    gl_accounts: PayCodingListDetail | None
    cost_centres: PayCodingListDetail | None
    can_manage: bool


class PaySuggestionOut(BaseModel):
    gl_code: str | None
    cost_centre: str | None
    confidence: float
    level: Literal["high", "medium", "low"]
    method: Literal["history", "model"]
    support: int
    # References of earlier invoices whose coding backs it.
    examples: list[str]
    reason: str | None = None


class PayLineCodingOut(BaseModel):
    line_index: int
    description: str | None
    gl_code: str | None
    cost_centre: str | None
    source: str | None
    corrected: bool
    coded_at: dt.datetime | None
    suggestion: PaySuggestionOut | None


class PayInvoiceCodingOut(BaseModel):
    invoice_id: uuid.UUID
    status: str
    gl_list_version: int | None
    cc_list_version: int | None
    gl_accounts: list[PayCodingEntryOut]
    cost_centres: list[PayCodingEntryOut]
    lines: list[PayLineCodingOut]
    can_code: bool
    auto_coding: bool
    checks_state: Literal["current", "stale", "failed", "missing"]


class PayModelLineOut(BaseModel):
    line_index: int
    suggestion: PaySuggestionOut | None
    # Why there is none: "no_model_suggestion" or "model_unavailable".
    problem: str | None
    from_recording: bool = False


class PayModelSuggestionsOut(BaseModel):
    lines: list[PayModelLineOut]


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


def _entry(e: PayCodingEntry) -> PayCodingEntryOut:
    return PayCodingEntryOut(code=e.code, name=e.name, name_ar=e.name_ar)


def _list(row: PayCodingList) -> PayCodingListOut:
    return PayCodingListOut(
        id=row.id,
        kind=row.kind,
        version=row.version,
        entry_count=row.entry_count,
        source=row.source,
        created_by_type=row.created_by_type,
        created_at=row.created_at,
    )


def _suggestion(found: Suggestion | None) -> PaySuggestionOut | None:
    if found is None:
        return None
    level = found.level
    return PaySuggestionOut(
        gl_code=found.gl_code,
        cost_centre=found.cost_centre,
        confidence=round(found.confidence, 3),
        level="high" if level == "high" else "medium" if level == "medium" else "low",
        method="model" if found.method == "model" else "history",
        support=found.support,
        examples=list(found.examples),
        reason=found.reason,
    )


def _problem(request: Request, exc: InvalidPurchaseDataError) -> Any:
    return problem_response(
        request, 400, exc.code, detail=str(exc), errors=[e.as_json() for e in exc.errors]
    )


async def _detail(session: OrgSession, kind: CodingKind, row: PayCodingList | None) -> Any:
    if row is None:
        return None
    entries = await entries_of(session, row)
    return PayCodingListDetail(**_list(row).model_dump(), entries=[_entry(e) for e in entries])


async def _invoice_coding(
    session: OrgSession, actor: Actor, view: CodingView, invoice_id: uuid.UUID
) -> PayInvoiceCodingOut:
    invoice = await get_invoice(session, invoice_id)
    return PayInvoiceCodingOut(
        invoice_id=invoice.id,
        status=invoice.status,
        gl_list_version=view.lists.gl.version if view.lists.gl else None,
        cc_list_version=view.lists.cc.version if view.lists.cc else None,
        gl_accounts=[_entry(e) for e in view.lists.gl_entries],
        cost_centres=[_entry(e) for e in view.lists.cc_entries],
        lines=[
            PayLineCodingOut(
                line_index=x.line.index,
                description=x.line.description,
                gl_code=x.coding.gl_code if x.coding else None,
                cost_centre=x.coding.cost_centre if x.coding else None,
                source=x.coding.source if x.coding else None,
                corrected=bool(x.coding.corrected) if x.coding else False,
                coded_at=x.coding.coded_at if x.coding else None,
                suggestion=_suggestion(x.suggestion),
            )
            for x in view.lines
        ],
        can_code=actor.has(INVOICE_PROCESS)
        and actor.kind is ActorKind.USER
        and invoice.status in {"needs_review", "ready"},
        auto_coding=view.auto_coding,
        checks_state=CHECKS_STATE[await checks_problem(session, invoice)],
    )


# ------------------------------------------------------------------ lists


@router.get("/coding", response_model=PayCodingListsOut)
async def get_lists(actor: CurrentActor, session: OrgSession) -> PayCodingListsOut:
    authorize(actor, INVOICE_READ)
    lists = await lists_in_force(session)
    return PayCodingListsOut(
        gl_accounts=await _detail(session, CodingKind.GL_ACCOUNT, lists.gl),
        cost_centres=await _detail(session, CodingKind.COST_CENTRE, lists.cc),
        can_manage=actor.has(CODING_MANAGE),
    )


@router.get("/coding/{list_path}/versions", response_model=list[PayCodingListOut])
async def get_versions(
    list_path: ListPath, actor: CurrentActor, session: OrgSession
) -> list[PayCodingListOut]:
    authorize(actor, INVOICE_READ)
    return [_list(row) for row in await list_versions(session, LIST_PATHS[list_path])]


@router.post("/coding/{list_path}", status_code=201, response_model=PayCodingListDetail)
async def create_version(
    request: Request,
    list_path: ListPath,
    body: PayCodingListIn,
    actor: CurrentActor,
    session: OrgSession,
) -> Any:
    authorize(actor, CODING_MANAGE)
    _person(actor)
    kind = LIST_PATHS[list_path]
    entries = [EntryInput(e.code, e.name, e.name_ar) for e in body.entries]
    try:
        row = await create_list(session, By.of(actor), kind, entries)
    except InvalidPurchaseDataError as exc:
        return _problem(request, exc)
    return await _detail(session, kind, row)


@router.post("/coding/{list_path}/import", status_code=201, response_model=PayCodingListDetail)
async def import_version(
    request: Request,
    list_path: ListPath,
    body: PayCodingCsvIn,
    actor: CurrentActor,
    session: OrgSession,
) -> Any:
    authorize(actor, CODING_MANAGE)
    _person(actor)
    kind = LIST_PATHS[list_path]
    try:
        row = await import_list(session, By.of(actor), kind, body.csv)
    except InvalidPurchaseDataError as exc:
        return _problem(request, exc)
    return await _detail(session, kind, row)


# ------------------------------------------------------------------ invoice lines


@router.get("/invoices/{invoice_id}/coding", response_model=PayInvoiceCodingOut)
async def get_invoice_coding(
    invoice_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayInvoiceCodingOut:
    authorize(actor, INVOICE_READ)
    invoice = await get_invoice(session, invoice_id)
    settings = await load_settings(session)
    view = await coding_view(session, invoice, auto_coding=settings.auto_code_high_confidence)
    return await _invoice_coding(session, actor, view, invoice.id)


@router.put("/invoices/{invoice_id}/coding/lines", response_model=PayInvoiceCodingOut)
async def put_invoice_coding(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceLinesCodingIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> Any:
    """A person codes lines; the checks run again when anything changed."""
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    items = [LineCodingInput(x.line_index, x.gl_code, x.cost_centre) for x in body.lines]
    try:
        result = await set_line_codings(session, actor, invoice_id, items)
    except InvalidPurchaseDataError as exc:
        return _problem(request, exc)
    if result.changed:
        await recheck_invoice(
            session,
            result.invoice,
            check_services(runtime, actor),
            trigger=RecheckTrigger.CODING,
            by=By.of(actor),
        )
        await refresh_decisions(session, result.invoice)
    settings = await load_settings(session)
    view = await coding_view(
        session, result.invoice, auto_coding=settings.auto_code_high_confidence
    )
    return await _invoice_coding(session, actor, view, invoice_id)


@router.post(
    "/invoices/{invoice_id}/coding/model-suggestions", response_model=PayModelSuggestionsOut
)
async def ask_model(
    invoice_id: uuid.UUID,
    body: PayModelSuggestionsIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayModelSuggestionsOut:
    """Model suggestions for lines the history cannot code. Nothing is stored or applied:
    the person sees them and codes the lines themselves."""
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await get_invoice(session, invoice_id)
    view = await coding_view(session, invoice)
    wanted = set(body.line_indexes)
    lines = [
        x
        for x in view.lines
        if (x.line.index in wanted)
        or (not wanted and (x.suggestion is None or x.suggestion.level != "high"))
    ][:MAX_MODEL_LINES]
    router = runtime.services.router
    out: list[PayModelLineOut] = []
    for x in lines:
        if router is None:
            out.append(
                PayModelLineOut(
                    line_index=x.line.index, suggestion=None, problem="model_unavailable"
                )
            )
            continue
        try:
            found = await model_suggestion(
                router, actor.org_id, invoice, x.line, view.lists, actor_id=actor.id
            )
        except NoModelSuggestionError:
            out.append(
                PayModelLineOut(
                    line_index=x.line.index, suggestion=None, problem="no_model_suggestion"
                )
            )
            continue
        except ModelUnavailable as exc:
            log.info("pay.coding.model_unavailable", error=type(exc).__name__)
            out.append(
                PayModelLineOut(
                    line_index=x.line.index, suggestion=None, problem="model_unavailable"
                )
            )
            continue
        out.append(
            PayModelLineOut(
                line_index=x.line.index,
                suggestion=_suggestion(found.suggestion),
                problem=None,
                from_recording=found.from_recording,
            )
        )
    return PayModelSuggestionsOut(lines=out)

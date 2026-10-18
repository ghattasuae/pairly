# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The journal export (``pairly_pay.journal``, ``pairly_pay.journal_formats``).

* ``POST /journal/export``  ``{"period": "YYYY-MM"}`` (``pay.journal.export``, a person
  with a second factor: an ``export`` action) the journal lines of the invoices approved
  and the payments marked paid in the period, as CSV, one balanced entry each. Every
  download is recorded (``pay_journal_exports``: period, format, SHA-256, counts, who)
  and audited; the digest is in ``X-Content-SHA256``.

  ``format`` picks the layout: ``generic`` (the default), ``xero``, ``quickbooks`` or
  ``zoho_books``. The last three need ``accounts``, the account of the accounting system
  for each journal account (the placeholders such as ``AP_CONTROL``, and any GL code to
  override): Xero takes the account code, so a GL code of the chart of accounts in force
  maps to itself; QuickBooks and Zoho Books take the account name, so a GL code maps to
  its name in the chart. ``tax_rate`` (Xero only) is the tax rate name for every line.
  A journal that cannot be written in the format (unbalanced, an account without a
  mapping, a value the format cannot hold) is refused with 422 and no file, no record.
* ``GET /journal/exports?period=YYYY-MM``  (``pay.month_end.read``) the downloads of a
  period, newest first.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_api.problems import problem_response
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.api.vendors import person
from pairly_pay.coding import current_list, entries_of
from pairly_pay.common import audit
from pairly_pay.journal import (
    AP_CONTROL,
    BANK,
    DISCOUNT_RECEIVED,
    SUSPENSE,
    VAT_INPUT,
    journal_lines,
)
from pairly_pay.journal_formats import (
    ExportRefusedError,
    MissingAccountMappingError,
    PayJournalFormat,
    render,
)
from pairly_pay.models import CodingKind, PayJournalExport
from pairly_pay.month_end import parse_period
from pairly_pay.permissions import JOURNAL_EXPORT, MONTH_END_READ

router = APIRouter(tags=["pay"])

PERIOD: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
EXPORT_ENTITY: Final = "pay.journal_export"
MAX_MAPPINGS: Final = 500
PLACEHOLDERS: Final = frozenset({AP_CONTROL, VAT_INPUT, BANK, DISCOUNT_RECEIVED, SUSPENSE})
# A journal account (a GL code or a placeholder) and the account it maps to: printable
# text, no control characters.
AccountKey = Annotated[
    str, StringConstraints(min_length=1, max_length=32, pattern=r"^[^\x00-\x1f\x7f]+$")
]
AccountValue = Annotated[
    str, StringConstraints(min_length=1, max_length=150, pattern=r"^[^\x00-\x1f\x7f]+$")
]


class PayJournalExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Annotated[str, Field(pattern=PERIOD)]
    format: PayJournalFormat = PayJournalFormat.GENERIC
    accounts: dict[AccountKey, AccountValue] | None = None
    tax_rate: AccountValue | None = None

    @model_validator(mode="after")
    def _options_fit_the_format(self) -> PayJournalExportIn:
        if self.accounts is not None and len(self.accounts) > MAX_MAPPINGS:
            raise ValueError(f"at most {MAX_MAPPINGS} account mappings")
        if self.format == PayJournalFormat.GENERIC and self.accounts:
            raise ValueError("the generic format takes no account mapping")
        if self.tax_rate is not None and self.format != PayJournalFormat.XERO:
            raise ValueError("tax_rate is for the xero format only")
        return self


class PayJournalExportOut(BaseModel):
    id: uuid.UUID
    period: str
    format: str
    sha256: str
    entry_count: int
    line_count: int
    exported_by: uuid.UUID
    created_at: datetime


@router.post(
    "/journal/export",
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_journal(
    request: Request, body: PayJournalExportIn, actor: CurrentActor, session: OrgSession
) -> Any:
    authorize(actor, JOURNAL_EXPORT)
    user_id = person(actor)
    period = parse_period(body.period)
    fmt = body.format
    given = dict(sorted((body.accounts or {}).items()))
    lines = await journal_lines(session, period)
    try:
        content = render(
            lines,
            fmt,
            accounts=await _account_map(session, fmt, given),
            tax_rate=body.tax_rate,
            number_prefix=f"PAIRLY-{period.label}-",
        )
    except ExportRefusedError as exc:
        missing = list(exc.accounts) if isinstance(exc, MissingAccountMappingError) else []
        return problem_response(request, 422, exc.code, detail=str(exc), accounts=missing)
    digest = hashlib.sha256(content).hexdigest()
    entries = len({x.entry for x in lines})
    record = PayJournalExport(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        period=period.label,
        format=fmt.value,
        sha256=digest,
        entry_count=entries,
        line_count=len(lines),
        exported_by=user_id,
        created_at=datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    await audit(
        session,
        actor,
        "pay.journal.exported",
        entity_type=EXPORT_ENTITY,
        entity_id=record.id,
        payload={
            "period": period.label,
            "format": fmt.value,
            "sha256": digest,
            "entries": entries,
            "lines": len(lines),
            **_mapping_audit(given),
            **({"tax_rate": body.tax_rate} if body.tax_rate is not None else {}),
        },
    )
    await session.commit()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{export_filename(period.label, fmt)}"',
            "X-Content-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )


def _mapping_audit(given: dict[str, str]) -> dict[str, Any]:
    """What the audit event keeps of the account matches: the placeholders' matches (five
    at most, so the payload stays small) and a SHA-256 of all of them."""
    if not given:
        return {}
    canonical = json.dumps(given, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "accounts": {k: v for k, v in given.items() if k in PLACEHOLDERS},
        "accounts_count": len(given),
        "accounts_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def export_filename(period: str, fmt: PayJournalFormat) -> str:
    """``journal-YYYY-MM.csv`` for the generic format, ``journal-YYYY-MM-<format>.csv``
    for the others."""
    suffix = "" if fmt == PayJournalFormat.GENERIC else f"-{fmt.value}"
    return f"journal-{period}{suffix}.csv"


async def _account_map(
    session: AsyncSession, fmt: PayJournalFormat, given: dict[str, str]
) -> dict[str, str]:
    """The account of the accounting system for each journal account: the GL codes of
    the chart of accounts in force (Xero by code, the others by name), then ``given``."""
    if fmt == PayJournalFormat.GENERIC:
        return {}
    chart = await entries_of(session, await current_list(session, CodingKind.GL_ACCOUNT))
    by_chart = {e.code: e.code if fmt == PayJournalFormat.XERO else e.name for e in chart}
    return {**by_chart, **given}


@router.get("/journal/exports", response_model=list[PayJournalExportOut])
async def list_exports(
    actor: CurrentActor,
    session: OrgSession,
    period: Annotated[str, Query(pattern=PERIOD)],
) -> list[PayJournalExportOut]:
    authorize(actor, MONTH_END_READ)
    rows = await session.scalars(
        select(PayJournalExport)
        .where(PayJournalExport.period == period)
        .order_by(PayJournalExport.created_at.desc(), PayJournalExport.id)
        .limit(50)
    )
    return [
        PayJournalExportOut(
            id=r.id,
            period=r.period,
            format=r.format,
            sha256=r.sha256,
            entry_count=r.entry_count,
            line_count=r.line_count,
            exported_by=r.exported_by,
            created_at=r.created_at,
        )
        for r in rows
    ]

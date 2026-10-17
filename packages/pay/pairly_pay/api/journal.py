# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The generic journal export (``pairly_pay.journal``).

* ``POST /journal/export``  ``{"period": "YYYY-MM"}`` (``pay.journal.export``, a person
  with a second factor: an ``export`` action) the journal lines of the invoices approved
  and the payments marked paid in the period, as CSV, one balanced entry each. Every
  download is recorded (``pay_journal_exports``: period, SHA-256, counts, who) and
  audited; the digest is in ``X-Content-SHA256``.
* ``GET /journal/exports?period=YYYY-MM``  (``pay.month_end.read``) the downloads of a
  period, newest first.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.api.vendors import person
from pairly_pay.common import audit
from pairly_pay.journal import journal_lines, to_csv
from pairly_pay.models import PayJournalExport
from pairly_pay.month_end import parse_period
from pairly_pay.permissions import JOURNAL_EXPORT, MONTH_END_READ

router = APIRouter(tags=["pay"])

PERIOD: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
EXPORT_ENTITY: Final = "pay.journal_export"
FORMAT: Final = "generic"


class PayJournalExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Annotated[str, Field(pattern=PERIOD)]


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
async def export_journal(body: PayJournalExportIn, actor: CurrentActor, session: OrgSession) -> Any:
    authorize(actor, JOURNAL_EXPORT)
    user_id = person(actor)
    period = parse_period(body.period)
    lines = await journal_lines(session, period)
    content = to_csv(lines)
    digest = hashlib.sha256(content).hexdigest()
    entries = len({x.entry for x in lines})
    record = PayJournalExport(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        period=period.label,
        format=FORMAT,
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
            "format": FORMAT,
            "sha256": digest,
            "entries": entries,
            "lines": len(lines),
        },
    )
    await session.commit()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="journal-{period.label}.csv"',
            "X-Content-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )


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

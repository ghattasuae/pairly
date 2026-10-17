# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay's sections of the shared dashboards (``pairly_core.dashboards``).

* Auditor: bank-detail change tickets of the last 90 days with their verification
  evidence: who verified and when, the result of each call back and the last four digits
  of the on-file number that was called (never the number, never the bank details), and
  whether the on-file number had changed shortly before (a fraud pattern).
* AI governance: guardrail findings on the last 30 days' invoices, by type.

Pay has no data request (export or delete) queue yet, so it adds nothing there.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Final

from sqlalchemy import select

from pairly_core.dashboards.model import (
    Board,
    DashboardSectionSpec,
    Item,
    Section,
    SectionContext,
    Stat,
    section,
)
from pairly_pay.models import (
    CallResult,
    PayBankChangeCall,
    PayBankChangeTicket,
    PayInvoice,
    TicketStatus,
)
from pairly_pay.permissions import BANK_CHANGE_READ, INVOICE_READ

TICKET_DAYS: Final = 90
GUARDRAIL_DAYS: Final = 30
# The guardrails flag a document at medium severity or worse (``GuardrailReport.flagged``).
MEDIUM_OR_WORSE: Final = frozenset({"medium", "high"})


async def bank_changes(ctx: SectionContext) -> Section:
    since = ctx.now - timedelta(days=TICKET_DAYS)
    tickets = list(
        await ctx.session.scalars(
            select(PayBankChangeTicket)
            .where(PayBankChangeTicket.created_at >= since)
            .order_by(PayBankChangeTicket.created_at.desc())
        )
    )
    calls: dict[object, list[PayBankChangeCall]] = defaultdict(list)
    if tickets:
        for call in await ctx.session.scalars(
            select(PayBankChangeCall)
            .where(PayBankChangeCall.ticket_id.in_([t.id for t in tickets]))
            .order_by(PayBankChangeCall.created_at)
        ):
            calls[call.ticket_id].append(call)
    items = []
    for t in tickets:
        made = calls.get(t.id, [])
        items.append(
            Item(
                kind="bank_change",
                id=str(t.id),
                label=t.reference,
                href=f"/pay/bank-changes/{t.id}",
                at=t.created_at,
                status=t.status,
                detail={
                    "verified": t.verified_at is not None,
                    "verified_at": t.verified_at.isoformat() if t.verified_at else None,
                    "calls": len(made),
                    "last_call": made[-1].result if made else None,
                    "called_last4": made[-1].number_last4 if made else None,
                    "phone_recently_changed": t.phone_recently_changed,
                    "approvers": len(t.approver_ids or []),
                    "outcome": t.outcome,
                },
            )
        )
    statuses = Counter(t.status for t in tickets)
    unverified_open = sum(
        1 for t in tickets if t.status == TicketStatus.AWAITING_VERIFICATION.value
    )
    confirmed = sum(
        1
        for t in tickets
        if any(c.result == CallResult.CONFIRMED.value for c in calls.get(t.id, []))
    )
    out = section("bank_changes", items, href="/pay/bank-changes")
    out.stats = [
        Stat("tickets_verified", float(sum(1 for t in tickets if t.verified_at is not None))),
        Stat("tickets_confirmed_by_call", float(confirmed)),
        Stat("tickets_awaiting_verification", float(unverified_open), href="/pay/bank-changes"),
        *(
            Stat(f"tickets_{name}", float(n), href="/pay/bank-changes")
            for name, n in sorted(statuses.items())
        ),
    ]
    return out


async def guardrails(ctx: SectionContext) -> Section:
    since = ctx.now - timedelta(days=GUARDRAIL_DAYS)
    reports = list(
        await ctx.session.scalars(
            select(PayInvoice.guardrail_report).where(PayInvoice.created_at >= since)
        )
    )
    by_type: Counter[str] = Counter()
    flagged = errors = checked = 0
    for report in reports:
        if not isinstance(report, dict):
            continue
        checked += 1
        findings = [f for f in report.get("findings") or [] if isinstance(f, dict)]
        serious = [f for f in findings if f.get("severity") in MEDIUM_OR_WORSE]
        flagged += int(bool(serious))
        errors += int(bool(report.get("errors")))
        by_type.update(str(f.get("type")) for f in serious if f.get("type"))
    return Section(
        key="guardrails_pay",
        count=flagged,
        href="/pay/exceptions",
        stats=[
            Stat("invoices_checked", float(checked)),
            Stat("guardrail_errors", float(errors)),
            *(Stat(f"finding_{kind}", float(n)) for kind, n in sorted(by_type.items())),
        ],
        source="pay_invoice_guardrail_reports",
        note=None if checked else "no_data",
    )


DASHBOARD_SECTIONS: Final[tuple[DashboardSectionSpec, ...]] = (
    DashboardSectionSpec(Board.AUDITOR, "bank_changes", BANK_CHANGE_READ, bank_changes),
    DashboardSectionSpec(Board.AI_GOVERNANCE, "guardrails_pay", INVOICE_READ, guardrails),
)

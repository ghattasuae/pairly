# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Per-organisation monthly model budget with a hard stop and alerts.

Spend is the sum of ``model_calls.cost_micros`` in the current UTC calendar month (the
single source of truth; nothing is double booked). The limit is the org's row in
``org_llm_budgets`` or, without one, ``PAIRLY_LLM_MONTHLY_BUDGET_USD``.

Atomicity (reserve, then settle):

1. ``reserve`` runs in its own short transaction, under a per-org transaction-level
   advisory lock. It reads ``spent`` and the unexpired ``reserved`` amounts, and admits
   the call only if ``spent + reserved + worst_case <= limit``; then it inserts a
   reservation for ``worst_case`` and commits before any provider request is sent.
   Otherwise it raises ``BudgetExceeded`` (fail closed) and nothing is sent.
2. ``settle`` runs in the transaction that inserts the ``model_calls`` row: under the
   same lock it deletes the reservation, so the actual cost replaces the worst case
   in one commit.

``worst_case`` is an upper bound: every input token priced at the highest input rate
(input tokens are bounded by the UTF-8 byte length of the prompt plus a fixed
overhead, since a token is at least one byte) and ``max_output_tokens`` output tokens
(the provider stops there). Because every admitted call holds its upper bound until it
settles, concurrent calls cannot overspend: total spend stays <= limit. The only way
past the limit is a provider reporting more tokens than the bound (a wrong estimate);
that excess is limited to the calls in flight at that moment, at most one per
concurrent caller, and the next reservation is refused.

Failed requests (HTTP errors) are not billed by providers and settle at zero. A
crashed caller leaves a reservation that stops counting at ``expires_at``.

Alerts: when a settled call moves spend across 80% or 100% of the limit, one row in
``llm_budget_alerts`` (unique per org, month and threshold, so each fires once even
under concurrency), one audit event ``core.llm.budget_threshold`` and a structured
warning log. A refused call when spend has already reached 100% also records the
100% alert once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog
from sqlalchemy import delete, func, insert, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnClause

from pairly_core.audit import ActorType, append_audit
from pairly_core.llm.errors import BudgetExceeded
from pairly_core.models.llm import LLMBudgetAlert, LLMBudgetReservation, ModelCall, OrgLLMBudget

log = structlog.get_logger("pairly_core.llm.budget")

LOCK_NAMESPACE = "pairly.llm.budget:"
ALERT_THRESHOLDS = (80, 100)
BUDGET_ALERT_ACTION = "core.llm.budget_threshold"
ROUTER_ACTOR = "model-router"

# Start of the current UTC month as a timestamptz, evaluated in SQL (transaction time).
_MONTH_START: ColumnClause[Any] = literal_column(
    "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
)


def usd_to_micros(value: Decimal) -> int:
    return int((value * 1_000_000).to_integral_value())


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    period: date
    limit_micros: int
    spent_micros: int
    reserved_micros: int


class Budget(Protocol):
    """What the router needs from a budget (``BudgetManager``, or a stub in tests)."""

    async def reserve(
        self, session: AsyncSession, org_id: uuid.UUID, amount_micros: int
    ) -> uuid.UUID: ...

    async def release(
        self, session: AsyncSession, org_id: uuid.UUID, reservation_id: uuid.UUID
    ) -> None: ...

    async def settle(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        reservation_id: uuid.UUID | None,
        cost_micros: int,
    ) -> None: ...


class BudgetManager:
    def __init__(self, *, default_limit_micros: int, reservation_ttl_seconds: int = 900) -> None:
        if default_limit_micros < 0:
            raise ValueError("default_limit_micros must be >= 0")
        self.default_limit_micros = default_limit_micros
        self.reservation_ttl = timedelta(seconds=reservation_ttl_seconds)

    async def _lock(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{LOCK_NAMESPACE}{org_id}"},
        )

    async def limit_for(self, session: AsyncSession, org_id: uuid.UUID) -> int:
        row = await session.scalar(
            select(OrgLLMBudget.monthly_limit_micros).where(OrgLLMBudget.org_id == org_id)
        )
        return self.default_limit_micros if row is None else int(row)

    async def status(self, session: AsyncSession, org_id: uuid.UUID) -> BudgetStatus:
        spent = await session.scalar(
            select(func.coalesce(func.sum(ModelCall.cost_micros), 0)).where(
                ModelCall.org_id == org_id, ModelCall.created_at >= _MONTH_START
            )
        )
        reserved = await session.scalar(
            select(func.coalesce(func.sum(LLMBudgetReservation.amount_micros), 0)).where(
                LLMBudgetReservation.org_id == org_id,
                LLMBudgetReservation.expires_at > func.now(),
            )
        )
        period = await session.scalar(
            select(literal_column("(date_trunc('month', now() AT TIME ZONE 'UTC'))::date"))
        )
        assert isinstance(period, date)  # noqa: S101  (SQL always returns a date here)
        return BudgetStatus(
            period=period,
            limit_micros=await self.limit_for(session, org_id),
            spent_micros=int(spent or 0),
            reserved_micros=int(reserved or 0),
        )

    async def reserve(
        self, session: AsyncSession, org_id: uuid.UUID, amount_micros: int
    ) -> uuid.UUID:
        """Admit a call costing at most ``amount_micros`` or raise ``BudgetExceeded``.

        ``session`` must carry the org's tenant context and be outside a transaction;
        this commits its own transaction (the reservation must be visible to others
        before the provider request goes out).
        """
        if amount_micros < 0:
            raise ValueError("amount_micros must be >= 0")
        refused: BudgetStatus | None = None
        async with session.begin():
            await self._lock(session, org_id)
            await session.execute(
                delete(LLMBudgetReservation).where(
                    LLMBudgetReservation.org_id == org_id,
                    LLMBudgetReservation.expires_at <= func.now(),
                )
            )
            current = await self.status(session, org_id)
            if (
                current.spent_micros + current.reserved_micros + amount_micros
                > current.limit_micros
            ):
                refused = current
                if current.spent_micros >= current.limit_micros:
                    await self._alert(session, org_id, current, current.spent_micros, 100)
            else:
                reservation_id = uuid.uuid4()
                await session.execute(
                    insert(LLMBudgetReservation).values(
                        id=reservation_id,
                        org_id=org_id,
                        amount_micros=amount_micros,
                        expires_at=func.now() + self.reservation_ttl,
                    )
                )
        if refused is not None:
            log.warning(
                "llm.budget.refused",
                org_id=str(org_id),
                period=refused.period.isoformat(),
                spent_micros=refused.spent_micros,
                reserved_micros=refused.reserved_micros,
                requested_micros=amount_micros,
                limit_micros=refused.limit_micros,
            )
            raise BudgetExceeded("the organisation's monthly model budget is used up")
        return reservation_id

    async def release(
        self, session: AsyncSession, org_id: uuid.UUID, reservation_id: uuid.UUID
    ) -> None:
        """Drop a reservation without spend (in the caller's transaction)."""
        await session.execute(
            delete(LLMBudgetReservation).where(
                LLMBudgetReservation.org_id == org_id, LLMBudgetReservation.id == reservation_id
            )
        )

    async def settle(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        reservation_id: uuid.UUID | None,
        cost_micros: int,
    ) -> None:
        """Call inside the transaction that inserted the ``model_calls`` row.

        Deletes the reservation and fires threshold alerts crossed by this call.
        """
        await self._lock(session, org_id)
        if reservation_id is not None:
            await self.release(session, org_id, reservation_id)
        if cost_micros <= 0:
            return
        current = await self.status(session, org_id)
        after = current.spent_micros
        before = after - cost_micros
        limit = current.limit_micros
        for pct in ALERT_THRESHOLDS:
            # Integer math: crossed when before < pct% of limit <= after.
            if before * 100 < pct * limit <= after * 100:
                await self._alert(session, org_id, current, after, pct)

    async def _alert(
        self, session: AsyncSession, org_id: uuid.UUID, current: BudgetStatus, spent: int, pct: int
    ) -> None:
        inserted = await session.scalar(
            pg_insert(LLMBudgetAlert)
            .values(
                org_id=org_id,
                period=current.period,
                threshold_pct=pct,
                spent_micros=spent,
                limit_micros=current.limit_micros,
            )
            .on_conflict_do_nothing(constraint="uq_llm_budget_alerts_org_id")
            .returning(LLMBudgetAlert.id)
        )
        if inserted is None:
            return
        payload = {
            "period": current.period.isoformat(),
            "threshold_pct": pct,
            "spent_micros": spent,
            "limit_micros": current.limit_micros,
        }
        await append_audit(
            session,
            action=BUDGET_ALERT_ACTION,
            actor_type=ActorType.SYSTEM,
            actor_id=ROUTER_ACTOR,
            entity_type="llm_budget",
            entity_id=current.period.isoformat(),
            payload=payload,
            org_id=org_id,
        )
        log.warning("llm.budget.threshold", org_id=str(org_id), **payload)


async def set_org_budget(session: AsyncSession, org_id: uuid.UUID, limit_micros: int) -> None:
    """Upsert the org's monthly limit (in the caller's transaction). Callers must check
    permissions first; this only writes."""
    if limit_micros < 0:
        raise ValueError("limit_micros must be >= 0")
    await session.execute(
        pg_insert(OrgLLMBudget)
        .values(org_id=org_id, monthly_limit_micros=limit_micros)
        .on_conflict_do_update(
            constraint="uq_org_llm_budgets_org_id",
            set_={"monthly_limit_micros": limit_micros, "updated_at": func.now()},
        )
    )

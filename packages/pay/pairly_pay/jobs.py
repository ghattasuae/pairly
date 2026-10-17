# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``pay_bank_change_activation``: new bank details become active after cooling-off.

Every five minutes the worker runs this plugin job. It finds the organisations with a
change ticket whose cooling-off period is over through
``pay_due_bank_change_org_ids()`` (SECURITY DEFINER, organisation ids only, migration
``pay_0001``), then works in each one under normal Row Level Security as the
application role: ``bank_changes.activate_due`` re-checks every ticket (approved by two
people, locked details unchanged) before it swaps the accounts and lifts the hold per
rule. A ticket that fails the checks fails closed (the hold stays, a person looks).
One organisation's failure never stops the others.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

import structlog
from sqlalchemy import text

from pairly_core.db import set_tenant
from pairly_core.jobs.registry import JobContext
from pairly_pay.bank_changes import activate_due

log = structlog.get_logger("pairly_pay.jobs")

ACTIVATION_JOB: Final = "pay_bank_change_activation"


async def _due_orgs(ctx: JobContext) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM pay_due_bank_change_org_ids() AS org_id")
        )
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def activate_org(ctx: JobContext, org_id: uuid.UUID) -> dict[str, int]:
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        return await activate_due(session)


async def activate_due_changes(ctx: JobContext) -> Mapping[str, Any]:
    totals = {"orgs": 0, "activated": 0, "failed": 0, "errors": 0}
    for org_id in await _due_orgs(ctx):
        totals["orgs"] += 1
        try:
            result = await activate_org(ctx, org_id)
        except Exception as exc:
            totals["errors"] += 1
            log.warning("pay_activation_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        totals["activated"] += result["activated"]
        totals["failed"] += result["failed"]
    return totals

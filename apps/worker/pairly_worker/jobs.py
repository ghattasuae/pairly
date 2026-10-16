# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Worker jobs.

``verify_audit_chains`` recomputes every organisation's audit hash chain. It runs nightly
(cron) and can be enqueued on demand. A broken chain produces:

* an alert log line (``audit_chain_alert``, level error) for log-based alerting, and
* an audit event ``core.audit.chain_verification_failed`` in that organisation's own
  chain, written by the ``system`` actor, so auditors see it in the log itself.

How organisations are found: the worker connects as the non-owner application role, for
which Row Level Security hides every row unless one organisation is selected. It calls
``audit_org_ids()``, a SECURITY DEFINER function from migration 0002 that returns only
the distinct organisation ids that have audit events. Each chain is then read through a
normal tenant-bound session. The worker never holds the owner credentials.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import (
    ActorType,
    AuditAction,
    ChainVerification,
    append_audit,
    verify_chain,
)
from pairly_core.db import set_tenant

log = structlog.get_logger("pairly_worker.jobs")

SYSTEM_ACTOR_ID = "pairly.worker.audit_verify"


async def list_audit_orgs(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Organisations that have audit events (ids only, via the SECURITY DEFINER function)."""
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM audit_org_ids() AS org_id"))
        return [uuid.UUID(str(value)) for value in rows.scalars()]


async def verify_org(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> ChainVerification:
    """Verify one chain; on failure, alert and record the failure in that chain."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            result = await verify_chain(session, org_id)
        if result.ok:
            return result
        log.error(
            "audit_chain_alert",
            alert="audit_chain_broken",
            org_id=str(org_id),
            broken_seq=result.broken_seq,
            reason=result.reason,
            checked=result.checked,
        )
        async with session.begin():
            await append_audit(
                session,
                action=AuditAction.AUDIT_CHAIN_BROKEN,
                actor_type=ActorType.SYSTEM,
                actor_id=SYSTEM_ACTOR_ID,
                entity_type="audit_chain",
                entity_id=str(org_id),
                payload={
                    "broken_seq": result.broken_seq,
                    "reason": result.reason,
                    "checked": result.checked,
                },
            )
        return result


async def verify_audit_chains(ctx: dict[str, Any]) -> dict[str, Any]:
    """Verify every organisation's chain. One failing org never stops the others."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    orgs = await list_audit_orgs(sessionmaker)
    broken: list[str] = []
    errors: list[str] = []
    for org_id in orgs:
        try:
            result = await verify_org(sessionmaker, org_id)
        except Exception as exc:
            # Fail closed: an org that cannot be verified is reported, never assumed fine.
            errors.append(str(org_id))
            log.error(
                "audit_chain_alert",
                alert="audit_chain_unverifiable",
                org_id=str(org_id),
                error=type(exc).__name__,
            )
            continue
        if not result.ok:
            broken.append(str(org_id))
    summary = {"orgs": len(orgs), "broken": broken, "errors": errors}
    if broken or errors:
        log.error("audit_verify_finished", **summary)
    else:
        log.info("audit_verify_finished", orgs=len(orgs))
    return summary

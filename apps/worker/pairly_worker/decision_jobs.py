# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision record and audit hardening jobs (WP1.10, WP1.10b).

Queued by the API:

* ``compliance_export``: build a compliance pack (``pairly_core.decisions.compliance``).
* ``verify_audit_chain``: verify one large chain and store the result.

Cron (UTC):

* ``audit_anchor_nightly``: sign each organisation's chain head and write it to the
  anchor store (``pairly_core.audit_anchor``). A head whose seq was anchored with a
  different hash is an alert: the chain was rewritten after it was anchored.
* ``appeal_sla_escalation`` (every 5 minutes): escalate overdue appeals.
* ``audit_denial_flush`` (every 5 minutes): write the summary event of rate-limited
  authorisation denials.
* ``decision_jobs_sweep`` (every minute): run export and verification jobs whose
  enqueue was lost or whose worker died.

Organisations are found through the SECURITY DEFINER functions of migrations 0002 and
0012 (ids only); each is processed in its own tenant-bound session, and one failing
organisation never stops the others.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from arq import cron, func
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit_anchor.anchoring import anchor_map, anchor_org, load_anchors, public_keys
from pairly_core.audit_anchor.denials import flush_suppressed
from pairly_core.audit_anchor.store import AnchorStore
from pairly_core.audit_anchor.verification import (
    AnchorLoader,
    give_up_verifications,
    run_verification,
    stale_verifications,
)
from pairly_core.db import set_tenant
from pairly_core.decisions.appeals import escalate_overdue_appeals
from pairly_core.decisions.compliance import give_up_exports, run_export, stale_exports
from pairly_core.decisions.jobs import COMPLIANCE_EXPORT_JOB, VERIFY_AUDIT_CHAIN_JOB
from pairly_worker.config import WorkerConfig

log = structlog.get_logger("pairly_worker.decision_jobs")

ANCHOR_JOB: Final = "audit_anchor_nightly"
APPEAL_SLA_JOB: Final = "appeal_sla_escalation"
DENIAL_FLUSH_JOB: Final = "audit_denial_flush"
DECISION_SWEEP_JOB: Final = "decision_jobs_sweep"
_ORG_FUNCTIONS: Final = frozenset(
    {
        "audit_org_ids",
        "appeal_overdue_org_ids",
        "decision_jobs_pending_org_ids",
        "audit_denial_flush_org_ids",
    }
)


async def _org_ids(
    sessionmaker: async_sessionmaker[AsyncSession], function: str
) -> list[uuid.UUID]:
    if function not in _ORG_FUNCTIONS:
        raise ValueError("unknown org listing function")
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text(f"SELECT org_id FROM {function}() AS org_id"))  # noqa: S608  # nosec B608 (fixed allowlist above)
        return [uuid.UUID(str(value)) for value in rows.scalars()]


def anchor_loader(ctx: dict[str, Any]) -> AnchorLoader | None:
    """Anchors checked with the worker's own key (the public half of the signing key)."""
    store: AnchorStore | None = ctx.get("anchor_store")
    key: Ed25519PrivateKey | None = ctx.get("audit_signing_key")
    if store is None or key is None:
        return None
    keys = public_keys(key.public_key())

    async def load(org_id: uuid.UUID) -> dict[int, str]:
        return anchor_map(await load_anchors(store, org_id, keys))

    return load


async def _each_org(
    ctx: dict[str, Any],
    function: str,
    work: Callable[[AsyncSession], Awaitable[int]],
    event: str,
) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    done = 0
    errors: list[str] = []
    for org_id in await _org_ids(sessionmaker, function):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    done += await work(session)
        except Exception as exc:
            errors.append(str(org_id))
            log.error(f"{event}_failed", org_id=str(org_id), error=type(exc).__name__)
    summary = {"done": done, "errors": errors}
    if done or errors:
        log.info(f"{event}_finished", **summary)
    return summary


# ------------------------------------------------------------------ queued jobs


async def compliance_export_job(ctx: dict[str, Any], org_id: str, export_id: str) -> dict[str, Any]:
    return await run_export(
        ctx["sessionmaker"],
        ctx.get("store"),
        uuid.UUID(org_id),
        uuid.UUID(export_id),
        anchors=anchor_loader(ctx),
    )


async def verify_audit_chain_job(
    ctx: dict[str, Any], org_id: str, verification_id: str
) -> dict[str, Any]:
    return await run_verification(
        ctx["sessionmaker"],
        uuid.UUID(org_id),
        uuid.UUID(verification_id),
        anchors=anchor_loader(ctx),
    )


# ------------------------------------------------------------------ cron


async def anchor_audit_chains(ctx: dict[str, Any]) -> dict[str, Any]:
    store: AnchorStore | None = ctx.get("anchor_store")
    key: Ed25519PrivateKey = ctx["audit_signing_key"]
    if store is None:
        log.warning("audit_anchor_skipped", reason="no anchor store configured")
        return {"skipped": True}
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    written: list[str] = []
    conflicts: list[str] = []
    errors: list[str] = []
    for org_id in await _org_ids(sessionmaker, "audit_org_ids"):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    result = await anchor_org(session, store, key)
        except Exception as exc:
            errors.append(str(org_id))
            log.error(
                "audit_chain_alert",
                alert="audit_anchor_failed",
                org_id=str(org_id),
                error=type(exc).__name__,
            )
            continue
        if result.status == "written":
            written.append(str(org_id))
        elif result.status == "conflict":
            conflicts.append(str(org_id))
            log.error(
                "audit_chain_alert",
                alert="audit_anchor_conflict",
                org_id=str(org_id),
                seq=result.seq,
            )
    summary = {"written": len(written), "conflicts": conflicts, "errors": errors}
    log.info("audit_anchor_finished", **summary)
    return summary


async def escalate_appeals(ctx: dict[str, Any]) -> dict[str, Any]:
    async def work(session: AsyncSession) -> int:
        return len(await escalate_overdue_appeals(session))

    return await _each_org(ctx, "appeal_overdue_org_ids", work, "appeal_escalation")


async def flush_denials(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _each_org(ctx, "audit_denial_flush_org_ids", flush_suppressed, "denial_flush")


async def sweep_decision_jobs(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    ran = 0
    errors: list[str] = []
    for org_id in await _org_ids(sessionmaker, "decision_jobs_pending_org_ids"):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    await give_up_exports(session)
                    await give_up_verifications(session)
                    exports = await stale_exports(session)
                    verifications = await stale_verifications(session)
            for export_id in exports:
                await compliance_export_job(ctx, str(org_id), str(export_id))
                ran += 1
            for verification_id in verifications:
                await verify_audit_chain_job(ctx, str(org_id), str(verification_id))
                ran += 1
        except Exception as exc:
            errors.append(str(org_id))
            log.error("decision_sweep_failed", org_id=str(org_id), error=type(exc).__name__)
    if ran or errors:
        log.info("decision_sweep_finished", ran=ran, errors=errors)
    return {"ran": ran, "errors": errors}


def functions() -> list[Any]:
    return [
        func(compliance_export_job, name=COMPLIANCE_EXPORT_JOB, max_tries=1, timeout=1800),
        func(verify_audit_chain_job, name=VERIFY_AUDIT_CHAIN_JOB, max_tries=1, timeout=3600),
    ]


def cron_jobs(config: WorkerConfig) -> list[Any]:
    return [
        cron(
            anchor_audit_chains,
            name=ANCHOR_JOB,
            hour=config.audit_anchor_hour,
            minute=config.audit_anchor_minute,
            run_at_startup=False,
            unique=True,
            timeout=1800,
        ),
        cron(
            escalate_appeals,
            name=APPEAL_SLA_JOB,
            minute=set(range(1, 60, 5)),
            run_at_startup=False,
            unique=True,
            timeout=240,
        ),
        cron(
            flush_denials,
            name=DENIAL_FLUSH_JOB,
            minute=set(range(2, 60, 5)),
            run_at_startup=False,
            unique=True,
            timeout=240,
        ),
        cron(
            sweep_decision_jobs,
            name=DECISION_SWEEP_JOB,
            run_at_startup=True,
            unique=True,
            timeout=3600,
        ),
    ]

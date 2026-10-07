# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Document jobs: virus scan, sandboxed parse, and the sweeper that re-queues stalls.

* ``scan_document(org_id, document_id)``  ClamAV scan. Clean documents queue a parse.
* ``parse_document(org_id, document_id)`` sandboxed parse of a clean document; a
  parsed policy document is then indexed for policy search (WP1.5).
* ``sweep_documents`` (cron, every minute) finds stalled documents through the
  ``documents_needing_work`` SECURITY DEFINER function (ids and status only) and queues
  them again, so a lost enqueue never leaves a document waiting forever.

Job arguments are strings (JSON serialised). The logic lives in ``pairly_core``
(``ingestion.scan`` and ``documents.pipeline``); these are thin wrappers.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.config import ConfigError, Environment
from pairly_core.documents.pipeline import ParseConfig, parse_document
from pairly_core.ingestion.queue import PARSE_JOB, SCAN_JOB
from pairly_core.ingestion.scan import ClamdScanner, UnavailableScanner, VirusScanner, scan_document
from pairly_core.ingestion.settings import IngestionSettings, load_ingestion_settings
from pairly_core.ingestion.storage import S3ObjectStore
from pairly_core.llm.settings import load_llm_settings
from pairly_core.policy.documents import index_after_parse
from pairly_core.retrieval.metered import build_metered_embedder
from pairly_core.search.documents import index_document_after_parse

log = structlog.get_logger("pairly_worker.documents")

SWEEP_LIMIT = 200
SWEEP_BUCKET_SECONDS = 600


def build_scanner(settings: IngestionSettings) -> VirusScanner:
    if not settings.clamav_host:
        log.warning("scanner_not_configured", effect="every document waits for review")
        return UnavailableScanner()
    return ClamdScanner(
        settings.clamav_host, settings.clamav_port, timeout=settings.clamav_timeout_seconds
    )


def setup_documents(ctx: dict[str, Any], env: Environment) -> None:
    """Fill ``ctx`` with the document services. Fails closed outside tests."""
    try:
        settings = load_ingestion_settings(env=env)
    except ConfigError:
        if env is Environment.TEST:
            log.warning("documents_disabled", reason="ingestion settings missing")
            return
        raise
    ctx["ingestion"] = settings
    ctx["store"] = S3ObjectStore.from_settings(settings)
    ctx["scanner"] = build_scanner(settings)
    ctx["parse_config"] = ParseConfig.from_settings(settings)
    ctx["parse_slots"] = asyncio.Semaphore(settings.parse_concurrency)
    # TODO(WP1.7): a VisionFallback backed by the model router.
    ctx["vision"] = None


async def _enqueue(ctx: dict[str, Any], job: str, org_id: str, document_id: str, key: str) -> None:
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        await redis.enqueue_job(job, org_id, document_id, _job_id=f"{job}:{document_id}:{key}")
    except Exception as exc:  # the sweeper recovers it
        log.warning("enqueue_failed", job=job, document_id=document_id, error=type(exc).__name__)


async def scan_document_job(ctx: dict[str, Any], org_id: str, document_id: str) -> dict[str, Any]:
    settings: IngestionSettings = ctx["ingestion"]
    result = await scan_document(
        ctx["sessionmaker"],
        ctx["store"],
        ctx["scanner"],
        org_id=uuid.UUID(org_id),
        document_id=uuid.UUID(document_id),
        max_attempts=settings.scan_max_attempts,
    )
    if result.parse_next:
        await _enqueue(ctx, PARSE_JOB, org_id, document_id, "0")
    return {"status": result.status, "reason": result.reason, "skipped": result.skipped}


async def parse_document_job(ctx: dict[str, Any], org_id: str, document_id: str) -> dict[str, Any]:
    slots: asyncio.Semaphore = ctx.setdefault("parse_slots", asyncio.Semaphore(1))
    async with slots:
        result = await parse_document(
            ctx["sessionmaker"],
            ctx["store"],
            org_id=uuid.UUID(org_id),
            document_id=uuid.UUID(document_id),
            config=ctx["parse_config"],
            vision=ctx.get("vision"),
        )
    if not result.skipped:
        await _index_policy(ctx, org_id, document_id)
        await _index_for_search(ctx, org_id, document_id)
    return {
        "status": result.status,
        "reason": result.reason,
        "needs_review": result.needs_review,
        "skipped": result.skipped,
    }


async def _index_policy(ctx: dict[str, Any], org_id: str, document_id: str) -> None:
    """Index the file if it is a policy document (WP1.5). Never fails the parse job:
    a policy document left ``pending`` can be indexed again from the API."""
    try:
        embedder = ctx.get("policy_embedder")
        if embedder is None:
            # Metered: budget reservation and a model_calls row per embedding call.
            embedder = ctx["policy_embedder"] = build_metered_embedder(
                load_llm_settings(), ctx["sessionmaker"]
            )
        await index_after_parse(
            ctx["sessionmaker"],
            ctx["store"],
            org_id=uuid.UUID(org_id),
            document_id=uuid.UUID(document_id),
            embedder=embedder,
        )
    except Exception as exc:
        log.warning("policy_index_failed", document_id=document_id, error=type(exc).__name__)


async def _index_for_search(ctx: dict[str, Any], org_id: str, document_id: str) -> None:
    """Index the parsed text for search if the organisation turned ``search.documents``
    on (WP1.12; PII redacted). Never fails the parse job."""
    try:
        embedder = ctx.get("policy_embedder")
        if embedder is None:
            embedder = ctx["policy_embedder"] = build_metered_embedder(
                load_llm_settings(), ctx["sessionmaker"]
            )
        outcome = await index_document_after_parse(
            ctx["sessionmaker"],
            ctx["store"],
            org_id=uuid.UUID(org_id),
            document_id=uuid.UUID(document_id),
            embedder=embedder,
        )
        if outcome.status == "failed":
            log.warning(
                "document_search_index_failed", document_id=document_id, reason=outcome.reason
            )
    except Exception as exc:
        log.warning(
            "document_search_index_failed", document_id=document_id, error=type(exc).__name__
        )


async def stalled_documents(
    sessionmaker: async_sessionmaker[AsyncSession], *, limit: int, max_scan_attempts: int
) -> list[tuple[str, str, str]]:
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id, document_id, status FROM documents_needing_work(:limit, :max)"),
            {"limit": limit, "max": max_scan_attempts},
        )
        return [(str(r.org_id), str(r.document_id), str(r.status)) for r in rows]


async def sweep_documents(ctx: dict[str, Any]) -> dict[str, int]:
    if "ingestion" not in ctx:
        return {"queued": 0}
    settings: IngestionSettings = ctx["ingestion"]
    stalled = await stalled_documents(
        ctx["sessionmaker"], limit=SWEEP_LIMIT, max_scan_attempts=settings.scan_max_attempts
    )
    # One sweep key per 10 minutes, so arq's job-id guard drops repeats within a window.
    key = f"sweep{int(time.time() // SWEEP_BUCKET_SECONDS)}"
    for org_id, document_id, status in stalled:
        job = PARSE_JOB if status == "clean" else SCAN_JOB
        await _enqueue(ctx, job, org_id, document_id, key)
    if stalled:
        log.info("documents_swept", queued=len(stalled))
    return {"queued": len(stalled)}

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``parse_document`` job: a clean document becomes ``parsed`` or ``parse_failed``.

1. Lock the row; only a ``clean`` document is parsed (anything else is skipped, so the
   job is safe to run twice).
2. Download the bytes into a private temp dir and verify their SHA-256.
3. Parse in the sandbox (``pairly_core.documents.sandbox``).
4. If OCR confidence is low, try the ``VisionFallback`` (when one is configured).
5. Store the ``NormalizedDocument`` JSON next to the original
   (``orgs/<org>/derived/<sha256>/normalized-<parser>-<version>.json``).
6. Record the outcome with an audit event. Any failure (crash, timeout, limit, invalid
   output, storage error) gives ``parse_failed`` with a reason and ``needs_review``.
   Review-worthy warnings (``REVIEW_WARNINGS``) also set ``needs_review``.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.documents.model import REVIEW_WARNINGS, NormalizedDocument, ParseWarning
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits, SandboxResult, run_sandboxed
from pairly_core.documents.vision import VisionFallback
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.service import DocumentAction
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.sniff import extension_for
from pairly_core.ingestion.storage import ObjectStore, StorageError, derived_key

log = structlog.get_logger(__name__)

PARSE_ACTOR: Final = "pairly.worker.parse"

Runner = Callable[..., Awaitable[SandboxResult]]


@dataclass(frozen=True, slots=True)
class ParseConfig:
    options: ParseOptions
    limits: SandboxLimits
    ocr_min_confidence: float = 60.0

    @classmethod
    def from_settings(cls, settings: IngestionSettings) -> ParseConfig:
        return cls(
            options=ParseOptions(
                ocr_languages=settings.ocr_languages,
                ocr_dpi=settings.ocr_dpi,
                max_pages=settings.parse_max_pages,
            ),
            limits=SandboxLimits(
                wall_seconds=settings.parse_wall_seconds,
                cpu_seconds=settings.parse_cpu_seconds,
                memory_bytes=settings.parse_memory_mb * 1024 * 1024,
                max_output_bytes=settings.parse_max_output_mb * 1024 * 1024,
            ),
            ocr_min_confidence=settings.ocr_min_confidence,
        )


@dataclass(frozen=True, slots=True)
class ParseJobResult:
    status: str
    reason: str | None = None
    needs_review: bool = False
    skipped: bool = False
    result_key: str | None = None


async def _locked(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _apply_vision(
    document: NormalizedDocument,
    *,
    path: Path,
    media_type: str,
    threshold: float,
    vision: VisionFallback | None,
) -> NormalizedDocument:
    low_pages = [
        p.number
        for p in document.pages
        if p.ocr is not None
        and (p.ocr.mean_confidence is None or p.ocr.mean_confidence < threshold)
    ]
    if not low_pages:
        return document
    if vision is not None:
        try:
            improved = await vision.transcribe(
                path=path, media_type=media_type, document=document, pages=low_pages
            )
        except Exception as exc:
            log.warning("vision_fallback_failed", error=type(exc).__name__)
            improved = None
        if improved is not None:
            return improved
    warning = ParseWarning(
        code="low_ocr_confidence",
        message=f"OCR confidence below {threshold:g} on pages {low_pages}",
    )
    return document.model_copy(update={"warnings": [*document.warnings, warning]})


async def parse_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    config: ParseConfig,
    vision: VisionFallback | None = None,
    runner: Runner = run_sandboxed,
) -> ParseJobResult:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            doc = await _locked(session, document_id)
            if doc is None:
                return ParseJobResult("missing", skipped=True)
            if doc.status != DocumentStatus.CLEAN:
                return ParseJobResult(doc.status, skipped=True)
            key, sha256, size, media_type = (
                doc.storage_key,
                doc.sha256,
                doc.size_bytes,
                doc.media_type,
            )

        reason: str | None = None
        document: NormalizedDocument | None = None
        result_key: str | None = None
        with tempfile.TemporaryDirectory(prefix="pairly-parse-") as tmp:
            workdir = Path(tmp)
            path = workdir / f"input{extension_for(media_type)}"
            try:
                await store.download_to(key, path, max_bytes=size)
                if await asyncio.to_thread(_sha256, path) != sha256:
                    reason = "integrity_mismatch"
            except StorageError:
                reason = "storage_error"
            if reason is None:
                try:
                    outcome = await runner(
                        path,
                        media_type,
                        workdir=workdir,
                        options=config.options,
                        limits=config.limits,
                    )
                except Exception as exc:  # the runner itself failing is a failed parse
                    log.error("sandbox_runner_error", error=type(exc).__name__)
                    outcome = SandboxResult(document=None, error="sandbox_error")
                if outcome.document is None or outcome.error is not None:
                    reason = outcome.error or "parse_error"
                else:
                    document = await _apply_vision(
                        outcome.document,
                        path=path,
                        media_type=media_type,
                        threshold=config.ocr_min_confidence,
                        vision=vision,
                    )
            if document is not None:
                result_key = derived_key(
                    org_id,
                    sha256,
                    f"normalized-{document.parser.name}-{document.parser.version}.json",
                )
                try:
                    await store.put_bytes(
                        result_key,
                        document.model_dump_json().encode("utf-8"),
                        content_type="application/json",
                    )
                except StorageError:
                    reason, document, result_key = "storage_error", None, None

        async with session.begin():
            doc = await _locked(session, document_id)
            if doc is None or doc.status != DocumentStatus.CLEAN:
                return ParseJobResult(doc.status if doc else "missing", skipped=True)
            return await _record(session, doc, document, reason, result_key)


async def _record(
    session: AsyncSession,
    doc: Document,
    document: NormalizedDocument | None,
    reason: str | None,
    result_key: str | None,
) -> ParseJobResult:
    now = datetime.now(UTC)
    if document is None:
        doc.status = DocumentStatus.PARSE_FAILED.value
        doc.status_reason = reason or "parse_error"
        doc.needs_review = True
        doc.parsed_at = now
        action = DocumentAction.PARSE_FAILED
        payload: dict[str, object] = {"reason": doc.status_reason}
    else:
        codes = document.warning_codes()
        review = bool(set(codes) & REVIEW_WARNINGS)
        doc.status = DocumentStatus.PARSED.value
        doc.parsed_at = now
        doc.parser = document.parser.name
        doc.parser_version = document.parser.version
        doc.page_count = document.page_count
        doc.parse_result_key = result_key
        doc.parse_warnings = codes
        if review:
            doc.needs_review = True
            doc.status_reason = "parse_warnings"
        action = DocumentAction.PARSED
        payload = {
            "parser": document.parser.name,
            "parser_version": document.parser.version,
            "pages": document.page_count,
            "warnings": codes,
        }
        confidence = document.ocr_confidence()
        if confidence is not None:
            payload["ocr_confidence"] = str(round(confidence, 2))
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=PARSE_ACTOR,
        entity_type="document",
        entity_id=doc.id,
        workflow=doc.workflow,
        payload=payload,
    )
    log.info("document_parsed", document_id=str(doc.id), status=doc.status, reason=reason)
    return ParseJobResult(
        doc.status,
        reason=doc.status_reason,
        needs_review=doc.needs_review,
        result_key=result_key,
    )

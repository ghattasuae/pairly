# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Virus scanning: a scanner interface, a ClamAV (clamd) client, a fake, and the scan job.

ClamAV runs as its own container (GPL, never linked). We speak the clamd protocol over
TCP: ``zINSTREAM\\0``, then chunks each prefixed with a 4-byte big-endian length, then a
zero-length chunk; the reply is ``stream: OK``, ``stream: <signature> FOUND`` or an
error, terminated by NUL.

Fail closed. Anything other than a clear ``OK`` is not clean:

* ``FOUND``: status ``infected``, ``needs_review`` set, the bytes are never served.
* clamd unreachable, timed out, size limit hit, unexpected reply, object missing or
  its hash does not match: status stays ``scanning``, ``needs_review`` is set with the
  reason, and the worker's sweeper retries later (up to ``scan_max_attempts``). A
  document is never marked clean because the scan could not run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import struct
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.service import DocumentAction
from pairly_core.ingestion.storage import ObjectNotFoundError, ObjectStore, StorageError

log = structlog.get_logger(__name__)

SCAN_ACTOR: Final = "pairly.worker.scan"
EICAR: Final = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
_CHUNK = 64 * 1024
_MAX_REPLY = 4096
# status_reason values a later clean scan resolves.
SCAN_ERROR_REASONS: Final = frozenset(
    {
        "scanner_unavailable",
        "scanner_timeout",
        "scanner_error",
        "scanner_size_limit",
        "scanner_not_configured",
        "scan_attempts_exhausted",
        "object_missing",
        "storage_error",
        "integrity_mismatch",
    }
)


class Verdict(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: Verdict
    engine: str | None = None
    signature: str | None = None
    # Machine-readable reason for ERROR: scanner_unavailable, scanner_timeout, ...
    reason: str | None = None


@runtime_checkable
class VirusScanner(Protocol):
    async def scan_file(self, path: Path) -> ScanResult:
        """Scan one file. Must return ERROR (never CLEAN) when the scan cannot run."""
        ...


class ClamdScanner:
    """clamd over TCP (INSTREAM)."""

    def __init__(
        self, host: str, port: int = 3310, *, timeout: float = 30.0, chunk_size: int = _CHUNK
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.chunk_size = chunk_size

    async def _command(self, command: bytes, path: Path | None = None) -> bytes:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(b"z" + command + b"\0")
            if path is not None:
                with path.open("rb") as handle:
                    while chunk := handle.read(self.chunk_size):
                        writer.write(struct.pack("!L", len(chunk)) + chunk)
                        await writer.drain()
                writer.write(struct.pack("!L", 0))
            await writer.drain()
            reply = await reader.readuntil(b"\0")
            if len(reply) > _MAX_REPLY:
                raise ValueError("reply too long")
            return reply.rstrip(b"\0")
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def version(self) -> str | None:
        try:
            async with asyncio.timeout(self.timeout):
                reply = await self._command(b"VERSION")
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            return None
        return reply.decode("utf-8", errors="replace")[:128] or None

    async def scan_file(self, path: Path) -> ScanResult:
        engine = await self.version()
        try:
            async with asyncio.timeout(self.timeout):
                reply = await self._command(b"INSTREAM", path)
        except TimeoutError:
            return ScanResult(Verdict.ERROR, engine, reason="scanner_timeout")
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            return ScanResult(Verdict.ERROR, engine, reason="scanner_unavailable")
        return parse_reply(reply, engine)


def parse_reply(reply: bytes, engine: str | None = None) -> ScanResult:
    """Map a clamd INSTREAM reply to a result. Only an exact ``OK`` is clean."""
    text = reply.decode("utf-8", errors="replace").strip()
    if "size limit" in text.lower():
        return ScanResult(Verdict.ERROR, engine, reason="scanner_size_limit")
    _, sep, status = text.partition(": ")
    if not sep:
        return ScanResult(Verdict.ERROR, engine, reason="scanner_error")
    if status == "OK":
        return ScanResult(Verdict.CLEAN, engine)
    if status.endswith(" FOUND"):
        signature = status[: -len(" FOUND")].strip()[:255] or "unknown"
        return ScanResult(Verdict.INFECTED, engine, signature=signature)
    return ScanResult(Verdict.ERROR, engine, reason="scanner_error")


class UnavailableScanner:
    """Used when no scanner is configured. Every scan errors (fail closed)."""

    async def scan_file(self, path: Path) -> ScanResult:
        return ScanResult(Verdict.ERROR, reason="scanner_not_configured")


@dataclass
class FakeScanner:
    """Test scanner: EICAR is infected; ``fail`` turns every scan into an error."""

    fail: str | None = None
    engine: str = "fake-scanner/1"
    calls: int = 0

    async def scan_file(self, path: Path) -> ScanResult:
        self.calls += 1
        if self.fail is not None:
            return ScanResult(Verdict.ERROR, self.engine, reason=self.fail)
        data = await asyncio.to_thread(path.read_bytes)
        if EICAR in data:
            return ScanResult(Verdict.INFECTED, self.engine, signature="Eicar-Test-Signature")
        return ScanResult(Verdict.CLEAN, self.engine)


# ------------------------------------------------------------------ job


@dataclass(frozen=True, slots=True)
class ScanJobResult:
    status: str
    reason: str | None = None
    # True when the document just became clean and should be parsed next.
    parse_next: bool = False
    skipped: bool = False


async def _locked(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def scan_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    scanner: VirusScanner,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    max_attempts: int = 5,
) -> ScanJobResult:
    """Scan one document and record the outcome. Safe to run twice (it skips done work)."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            doc = await _locked(session, document_id)
            if doc is None:
                return ScanJobResult("missing", skipped=True)
            if doc.status not in {DocumentStatus.RECEIVED, DocumentStatus.SCANNING}:
                return ScanJobResult(doc.status, skipped=True)
            if doc.scan_attempts >= max_attempts:
                return ScanJobResult(doc.status, reason="scan_attempts_exhausted", skipped=True)
            doc.status = DocumentStatus.SCANNING.value
            doc.scan_attempts += 1
            doc.last_scan_at = datetime.now(UTC)
            key, size, sha256 = doc.storage_key, doc.size_bytes, doc.sha256
            attempt = doc.scan_attempts

        result = await _scan_object(store, scanner, key=key, size=size, sha256=sha256)

        async with session.begin():
            doc = await _locked(session, document_id)
            if doc is None or doc.status != DocumentStatus.SCANNING:
                return ScanJobResult(doc.status if doc else "missing", skipped=True)
            return await _record(session, doc, result, attempt=attempt, max_attempts=max_attempts)


async def _scan_object(
    store: ObjectStore, scanner: VirusScanner, *, key: str, size: int, sha256: str
) -> ScanResult:
    with tempfile.TemporaryDirectory(prefix="pairly-scan-") as tmp:
        path = Path(tmp) / "object"
        try:
            await store.download_to(key, path, max_bytes=size)
        except ObjectNotFoundError:
            return ScanResult(Verdict.ERROR, reason="object_missing")
        except StorageError:
            return ScanResult(Verdict.ERROR, reason="storage_error")
        digest = await asyncio.to_thread(_sha256_file, path)
        if digest != sha256:
            return ScanResult(Verdict.ERROR, reason="integrity_mismatch")
        try:
            return await scanner.scan_file(path)
        except Exception as exc:  # a scanner bug must never look like a clean scan
            log.error("scanner_crashed", error=type(exc).__name__)
            return ScanResult(Verdict.ERROR, reason="scanner_error")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _record(
    session: AsyncSession, doc: Document, result: ScanResult, *, attempt: int, max_attempts: int
) -> ScanJobResult:
    now = datetime.now(UTC)
    payload: dict[str, object] = {"attempt": attempt}
    if result.engine:
        payload["engine"] = result.engine
    if result.verdict is Verdict.CLEAN:
        doc.status = DocumentStatus.CLEAN.value
        doc.scanned_at = now
        doc.scan_engine = result.engine
        # A clean scan resolves an earlier "scanner unavailable" flag.
        if doc.status_reason in SCAN_ERROR_REASONS:
            doc.needs_review = False
        doc.status_reason = None
        action = DocumentAction.SCAN_CLEAN
        outcome = ScanJobResult(doc.status, parse_next=True)
    elif result.verdict is Verdict.INFECTED:
        doc.status = DocumentStatus.INFECTED.value
        doc.scanned_at = now
        doc.scan_engine = result.engine
        doc.scan_signature = result.signature
        doc.needs_review = True
        doc.status_reason = "infected"
        payload["signature"] = result.signature
        action = DocumentAction.SCAN_INFECTED
        outcome = ScanJobResult(doc.status, reason="infected")
    else:
        reason = result.reason or "scanner_error"
        if attempt >= max_attempts:
            reason = "scan_attempts_exhausted"
        doc.needs_review = True
        doc.status_reason = reason
        payload["reason"] = result.reason or "scanner_error"
        action = DocumentAction.SCAN_FAILED
        outcome = ScanJobResult(doc.status, reason=reason)
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=SCAN_ACTOR,
        entity_type="document",
        entity_id=doc.id,
        workflow=doc.workflow,
        payload=payload,
    )
    log.info(
        "document_scanned",
        document_id=str(doc.id),
        verdict=result.verdict.value,
        reason=outcome.reason,
    )
    return outcome

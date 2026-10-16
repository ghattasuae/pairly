# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Upload plumbing shared by the routes that accept files (``/v1/documents`` and
``/v1/policies/documents``).

* ``DocumentServices`` / ``get_services``: storage, queue and limits (``app.state``).
* ``receive_upload``: streams a multipart body with exactly one ``file`` part to a
  private temp file, hashing and counting as it goes, stopping at the size limit.
  ``receive_upload_with_fields`` also returns the small text fields a route names
  (for example a note next to a receipt); every other field is still discarded.
* ``rejected_response``: the problem response for a rejected file (413, 415, 400).
* ``enqueue_scan``: queues the virus scan after the commit.
* ``DocumentOut``: the document record both upload endpoints return.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from pairly_api.problems import problem_response
from pairly_core.errors import BadRequestError, ServiceUnavailableError
from pairly_core.ingestion.models import Document
from pairly_core.ingestion.queue import SCAN_JOB, JobQueue
from pairly_core.ingestion.service import RejectReason, SpooledFile, SpoolWriter, TooLargeError
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import ObjectStore

FILE_FIELD = "file"
# Room for the multipart framing around the file part.
MULTIPART_OVERHEAD = 64 * 1024
MAX_PARTS = 8
MAX_FIELD_BYTES = 4096


@dataclass(frozen=True, slots=True)
class DocumentServices:
    """Storage, queue and limits for the document routes (``app.state.documents``)."""

    settings: IngestionSettings
    store: ObjectStore
    queue: JobQueue
    tmp_dir: Path | None = None


def get_services(request: Request) -> DocumentServices:
    services = getattr(request.app.state, "documents", None)
    if not isinstance(services, DocumentServices):
        raise ServiceUnavailableError("document storage is not configured")
    return services


# ------------------------------------------------------------------ schemas


class DocumentOut(BaseModel):
    id: uuid.UUID
    workflow: str
    source: str
    original_filename: str | None
    media_type: str
    size_bytes: int
    sha256: str
    status: str
    status_reason: str | None
    needs_review: bool
    submitted_by: uuid.UUID | None
    inbound_email_id: uuid.UUID | None
    page_count: int | None
    parser: str | None
    parse_warnings: list[str]
    created_at: datetime
    updated_at: datetime
    scanned_at: datetime | None
    parsed_at: datetime | None

    @classmethod
    def of(cls, doc: Document) -> DocumentOut:
        return cls(
            id=doc.id,
            workflow=doc.workflow,
            source=doc.source,
            original_filename=doc.original_filename,
            media_type=doc.media_type,
            size_bytes=doc.size_bytes,
            sha256=doc.sha256,
            status=doc.status,
            status_reason=doc.status_reason,
            needs_review=doc.needs_review,
            submitted_by=doc.submitted_by,
            inbound_email_id=doc.inbound_email_id,
            page_count=doc.page_count,
            parser=doc.parser,
            parse_warnings=list(doc.parse_warnings or []),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            scanned_at=doc.scanned_at,
            parsed_at=doc.parsed_at,
        )


# ------------------------------------------------------------------ multipart


class _Receiver:
    """Callbacks for the streaming multipart parser: one ``file`` part, spooled, plus
    the text fields named in ``text_fields`` (each once, within ``MAX_FIELD_BYTES``)."""

    def __init__(
        self, tmp: Path, max_bytes: int, text_fields: frozenset[str] = frozenset()
    ) -> None:
        self.tmp = tmp
        self.max_bytes = max_bytes
        self.text_fields = text_fields
        self.writer: SpoolWriter | None = None
        self.filename: str | None = None
        self.parts = 0
        self.files = 0
        self.fields: dict[str, bytearray] = {}
        self._headers: dict[bytes, bytes] = {}
        self._field = b""
        self._value = b""
        self._in_file = False
        self._text: bytearray | None = None
        self._field_bytes = 0

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self.parts += 1
        if self.parts > MAX_PARTS:
            raise BadRequestError("too many form parts")
        self._headers = {}
        self._in_file = False
        self._text = None

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", errors="replace")
        if name != FILE_FIELD:
            if name in self.text_fields and b"filename" not in params:
                if name in self.fields:
                    raise BadRequestError(f"send the '{name}' field once")
                self._text = self.fields[name] = bytearray()
            return
        self.files += 1
        if self.files > 1:
            raise BadRequestError("send exactly one file part")
        raw_name = params.get(b"filename")
        self.filename = raw_name.decode("utf-8", errors="replace") if raw_name else None
        self.writer = SpoolWriter(self.tmp, max_bytes=self.max_bytes)
        self._in_file = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file and self.writer is not None:
            self.writer.write(data[start:end])
            return
        self._field_bytes += end - start
        if self._field_bytes > MAX_FIELD_BYTES:
            raise BadRequestError("form fields too large")
        if self._text is not None:
            self._text += data[start:end]

    def _part_end(self) -> None:
        self._in_file = False
        self._text = None

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.abort()


@dataclass(frozen=True, slots=True)
class ReceivedUpload:
    file: SpooledFile
    filename: str | None
    # The requested text fields that were sent, decoded as UTF-8 (strict).
    fields: dict[str, str]


async def receive_upload(
    request: Request, tmp: Path, max_bytes: int, timeout: float
) -> tuple[SpooledFile, str | None]:
    received = await receive_upload_with_fields(request, tmp, max_bytes, timeout)
    return received.file, received.filename


async def receive_upload_with_fields(
    request: Request,
    tmp: Path,
    max_bytes: int,
    timeout: float,
    text_fields: frozenset[str] = frozenset(),
) -> ReceivedUpload:
    """Like ``receive_upload``, plus the text fields named in ``text_fields``."""
    content_type = request.headers.get("content-type", "")
    media, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise UnsupportedBodyError()
    receiver = _Receiver(tmp, max_bytes, text_fields)
    parser = MultipartParser(boundary, receiver.callbacks())  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                parser.write(chunk)
        parser.finalize()
    except TimeoutError:
        receiver.abort()
        raise BadRequestError("upload took too long") from None
    except MultipartParseError:
        receiver.abort()
        raise BadRequestError("malformed multipart body") from None
    except BaseException:
        receiver.abort()
        raise
    if receiver.writer is None:
        raise BadRequestError(f"a '{FILE_FIELD}' file part is required")
    try:
        fields = {name: bytes(value).decode("utf-8") for name, value in receiver.fields.items()}
    except UnicodeDecodeError:
        receiver.abort()
        raise BadRequestError("form fields must be UTF-8 text") from None
    return ReceivedUpload(receiver.writer.finish(), receiver.filename, fields)


class UnsupportedBodyError(Exception):
    pass


REJECT_STATUS = {
    RejectReason.TOO_LARGE: (413, "payload_too_large", "File too large"),
    RejectReason.TYPE_NOT_ALLOWED: (415, "unsupported_media_type", "File type not allowed"),
    RejectReason.EMPTY: (400, "empty_file", "File is empty"),
}


def rejected_response(request: Request, reason: RejectReason, workflow: str) -> JSONResponse:
    status, code, title = REJECT_STATUS.get(reason, (400, "rejected", "File rejected"))
    return problem_response(
        request,
        status,
        code,
        title=title,
        detail=f"{title} for workflow {workflow}",
    )


async def enqueue_scan(queue: JobQueue, org_id: uuid.UUID, document_id: uuid.UUID) -> None:
    # After the commit. A lost enqueue is recovered by the worker's sweeper.
    await queue.enqueue(SCAN_JOB, org_id=org_id, document_id=document_id, key="0")


# ------------------------------------------------------------------ batch multipart


@dataclass(frozen=True, slots=True)
class ReceivedPart:
    """One ``file`` part of a batch: spooled, or ``None`` when it passed the size limit."""

    file: SpooledFile | None
    filename: str | None


@dataclass(frozen=True, slots=True)
class ReceivedBatch:
    parts: list[ReceivedPart]
    fields: dict[str, str]


class _BatchReceiver:
    """Callbacks for a batch body: up to ``max_files`` ``file`` parts, each spooled to
    its own private file within ``max_bytes`` (a larger one is dropped and reported as
    too large, the others still count), plus the text fields in ``text_fields``."""

    def __init__(
        self,
        tmp: Path,
        max_bytes: int,
        max_files: int,
        text_fields: frozenset[str],
        max_field_bytes: int,
    ) -> None:
        self.tmp = tmp
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.text_fields = text_fields
        self.max_field_bytes = max_field_bytes
        self.parts: list[tuple[SpoolWriter | None, str | None, bool]] = []
        self.fields: dict[str, bytearray] = {}
        self._count = 0
        self._headers: dict[bytes, bytes] = {}
        self._field = b""
        self._value = b""
        self._writer: SpoolWriter | None = None
        self._name: str | None = None
        self._in_file = False
        self._too_large = False
        self._text: bytearray | None = None
        self._field_bytes = 0

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._count += 1
        if self._count > self.max_files + len(self.text_fields) + 2:
            raise BadRequestError("too many form parts")
        self._headers = {}
        self._in_file = False
        self._text = None

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", errors="replace")
        if name != FILE_FIELD:
            if name in self.text_fields and b"filename" not in params:
                if name in self.fields:
                    raise BadRequestError(f"send the '{name}' field once")
                self._text = self.fields[name] = bytearray()
            return
        if len(self.parts) >= self.max_files:
            raise BadRequestError(f"a batch has at most {self.max_files} files")
        raw_name = params.get(b"filename")
        self._name = raw_name.decode("utf-8", errors="replace") if raw_name else None
        self._writer = SpoolWriter(self.tmp, max_bytes=self.max_bytes)
        self._too_large = False
        self._in_file = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file:
            if self._writer is None:
                return  # already too large: skip the rest of this part
            try:
                self._writer.write(data[start:end])
            except TooLargeError:
                self._writer.abort()
                self._writer = None
                self._too_large = True
            return
        self._field_bytes += end - start
        if self._field_bytes > self.max_field_bytes:
            raise BadRequestError("form fields too large")
        if self._text is not None:
            self._text += data[start:end]

    def _part_end(self) -> None:
        if self._in_file:
            self.parts.append((self._writer, self._name, self._too_large))
            self._writer = None
        self._in_file = False
        self._text = None

    def abort(self) -> None:
        if self._writer is not None:
            self._writer.abort()
        for writer, _, _ in self.parts:
            if writer is not None:
                writer.abort()


async def receive_batch(
    request: Request,
    tmp: Path,
    *,
    max_bytes: int,
    max_files: int,
    timeout: float,
    text_fields: frozenset[str],
    max_field_bytes: int,
) -> ReceivedBatch:
    """Stream a multipart batch body into private spool files (see ``_BatchReceiver``)."""
    content_type = request.headers.get("content-type", "")
    media, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise UnsupportedBodyError()
    receiver = _BatchReceiver(tmp, max_bytes, max_files, text_fields, max_field_bytes)
    parser = MultipartParser(boundary, receiver.callbacks())  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                parser.write(chunk)
        parser.finalize()
    except TimeoutError:
        receiver.abort()
        raise BadRequestError("upload took too long") from None
    except MultipartParseError:
        receiver.abort()
        raise BadRequestError("malformed multipart body") from None
    except BaseException:
        receiver.abort()
        raise
    try:
        fields = {name: bytes(value).decode("utf-8") for name, value in receiver.fields.items()}
    except UnicodeDecodeError:
        receiver.abort()
        raise BadRequestError("form fields must be UTF-8 text") from None
    parts = [
        ReceivedPart(file=None if too_large or writer is None else writer.finish(), filename=name)
        for writer, name, too_large in receiver.parts
    ]
    return ReceivedBatch(parts=parts, fields=fields)

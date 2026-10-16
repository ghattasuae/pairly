# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Development only: serve the filesystem object store's signed GET URLs.

``GET /v1/_dev/objects/<key>?expires=&type=&disposition=&sig=`` stands in for a MinIO
presigned URL when the native stack runs without MinIO (``PAIRLY_OBJECT_STORE=filesystem``,
``pairly_core.ingestion.fs_storage``). The factory mounts this router only when
``PAIRLY_ENV`` is ``development`` or ``test`` and the filesystem store is configured, and
every request checks again that the store in use is the filesystem store.

Same rules as a presigned S3 URL: no session or token, the signature is the
authorisation (it was issued after an RBAC check, see ``routes.documents``), it expires
within minutes, and the media type and ``Content-Disposition`` (always ``attachment``)
come from the signed parameters, so none of them can be changed. The bytes are checked
against their recorded SHA-256 before they are served.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from pairly_core.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from pairly_core.ingestion.fs_storage import FilesystemObjectStore
from pairly_core.ingestion.storage import ObjectNotFoundError, StorageError

# Larger than any upload or derived artifact the stack writes.
MAX_SERVED_BYTES = 512 * 1024 * 1024

router = APIRouter(prefix="/_dev/objects", include_in_schema=False)


def _store(request: Request) -> FilesystemObjectStore:
    services = getattr(request.app.state, "documents", None)
    store = getattr(services, "store", None)
    if not isinstance(store, FilesystemObjectStore):
        raise NotFoundError("not found")
    return store


@router.get("/{key:path}")
async def get_object(
    request: Request,
    key: str,
    expires: int = Query(),
    type: str = Query(max_length=255),
    disposition: str = Query(max_length=2048),
    sig: str = Query(min_length=64, max_length=64),
) -> Response:
    store = _store(request)
    if not store.verify_signature(
        key, expires=expires, content_type=type, disposition=disposition, sig=sig
    ):
        raise ForbiddenError("the signed URL is invalid or has expired")
    try:
        data, _stored_type = await store.read_object(key, max_bytes=MAX_SERVED_BYTES)
    except ObjectNotFoundError:
        raise NotFoundError("not found") from None
    except StorageError:
        raise ServiceUnavailableError("the object failed its integrity check") from None
    return Response(
        content=data,
        media_type=type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, no-store",
        },
    )

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/_dev/objects``: the filesystem object store's signed URLs (native development).
No database needed."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import SecretStr

from pairly_api.main import _serves_dev_objects
from pairly_api.problems import install_error_handlers
from pairly_api.routes import dev_objects
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.ingestion.fs_storage import FilesystemObjectStore, url_signing_key
from pairly_core.ingestion.queue import NullJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore, blob_key

from .conftest import make_settings

SECRET = "dev-object-store-secret-for-tests"  # noqa: S105 (test value)


def _ingestion() -> IngestionSettings:
    return IngestionSettings(
        env=Environment.TEST,
        s3_bucket="pairly-documents",
        s3_access_key=SecretStr("pairly-app"),
        s3_secret_key=SecretStr(SECRET),
    )


def _app(store: object) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    v1 = APIRouter(prefix="/v1")
    v1.include_router(dev_objects.router)
    app.include_router(v1)
    app.state.documents = DocumentServices(
        settings=_ingestion(),
        store=store,  # type: ignore[arg-type]
        queue=NullJobQueue(),
    )
    return app


@pytest.fixture
def store(tmp_path: Path) -> FilesystemObjectStore:
    return FilesystemObjectStore(
        root=tmp_path,
        bucket="pairly-documents",
        signing_key=url_signing_key(SECRET),
        public_base_url="http://testserver",
    )


async def _get(app: FastAPI, url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(url)


async def test_serves_a_signed_url(store: FilesystemObjectStore) -> None:
    data = b"%PDF-1.7 synthetic"
    key = blob_key(uuid.uuid4(), hashlib.sha256(data).hexdigest())
    await store.put_bytes(key, data, content_type="application/pdf")
    url = store.presigned_get_url(
        key, expires_in=60, filename="receipt.pdf", content_type="application/pdf"
    )
    response = await _get(_app(store), url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith('attachment; filename="receipt.pdf"')
    assert response.headers["cache-control"] == "private, no-store"


async def test_refuses_changed_expired_or_missing(store: FilesystemObjectStore) -> None:
    data = b"bytes"
    key = blob_key(uuid.uuid4(), hashlib.sha256(data).hexdigest())
    await store.put_bytes(key, data, content_type="application/pdf")
    url = store.presigned_get_url(key, expires_in=60, filename=None, content_type="application/pdf")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    app = _app(store)

    changed = query | {"type": "text/html"}
    response = await _get(app, f"{parts.path}?{urlencode(changed)}")
    assert response.status_code == 403
    expired = query | {"expires": "1000"}
    assert (await _get(app, f"{parts.path}?{urlencode(expired)}")).status_code == 403
    assert (await _get(app, parts.path)).status_code in {400, 422}

    other_key = blob_key(uuid.uuid4(), "e" * 64)
    missing = store.presigned_get_url(
        other_key, expires_in=60, filename=None, content_type="application/pdf"
    )
    assert (await _get(app, missing)).status_code == 404

    # Tampered bytes on disk are never served.
    store._path(key).write_bytes(b"BYTES")
    assert (await _get(app, url)).status_code == 503


async def test_not_served_by_another_store(store: FilesystemObjectStore) -> None:
    data = b"bytes"
    key = blob_key(uuid.uuid4(), hashlib.sha256(data).hexdigest())
    url = store.presigned_get_url(key, expires_in=60, filename=None, content_type="application/pdf")
    assert (await _get(_app(InMemoryObjectStore()), url)).status_code == 404


def test_mounted_only_in_development_and_test_with_the_filesystem_store(
    store: FilesystemObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs_docs = DocumentServices(settings=_ingestion(), store=store, queue=NullJobQueue())
    memory_docs = DocumentServices(
        settings=_ingestion(), store=InMemoryObjectStore(), queue=NullJobQueue()
    )
    test_settings = make_settings()
    assert _serves_dev_objects(test_settings, fs_docs)
    assert not _serves_dev_objects(test_settings, memory_docs)
    production = test_settings.model_copy(update={"env": Environment.PRODUCTION})
    assert not _serves_dev_objects(production, fs_docs)
    monkeypatch.setenv("PAIRLY_OBJECT_STORE", "filesystem")
    assert _serves_dev_objects(test_settings, None)
    assert not _serves_dev_objects(production, None)
    monkeypatch.setenv("PAIRLY_OBJECT_STORE", "s3")
    assert not _serves_dev_objects(test_settings, None)

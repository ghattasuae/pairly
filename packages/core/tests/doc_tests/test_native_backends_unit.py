# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Native development backends (no MinIO, Mailpit or ClamAV): the filesystem object
store, write-once filesystem anchors, the email outbox and the dev-fake scanner. Each is
refused outside development and test."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from email import message_from_bytes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from pairly_core.audit_anchor.store import (
    FilesystemAnchorStore,
    anchor_key,
    anchor_store_from_settings,
    load_anchor_settings,
    org_prefix,
)
from pairly_core.config import ConfigError, Environment
from pairly_core.ingestion.fs_storage import (
    DEV_OBJECTS_PATH,
    FilesystemObjectStore,
    url_signing_key,
)
from pairly_core.ingestion.scan import (
    DEV_FAKE_ENGINE,
    EICAR,
    ClamdScanner,
    FakeScanner,
    Verdict,
    scanner_from_settings,
)
from pairly_core.ingestion.settings import IngestionSettings, load_ingestion_settings
from pairly_core.ingestion.storage import (
    ObjectNotFoundError,
    S3ObjectStore,
    StorageError,
    blob_key,
    derived_key,
    object_store_from_settings,
)
from pairly_core.notifications.email import OutgoingEmail, email_transport_from_settings
from pairly_core.notifications.outbox import INDEX_FILE, OutboxTransport
from pairly_core.notifications.settings import NotificationSettings, load_notification_settings

SECRET = "a-long-enough-store-secret-for-tests"  # noqa: S105 (test value)
ORG = uuid.uuid4()


def _ingestion(env: Environment, **extra: Any) -> IngestionSettings:
    base: dict[str, Any] = {
        "env": env,
        "s3_bucket": "pairly-documents",
        "s3_access_key": "pairly-app",
        "s3_secret_key": SECRET,
    }
    return IngestionSettings(**(base | extra))


def _fs_store(tmp_path: Path) -> FilesystemObjectStore:
    return FilesystemObjectStore(
        root=tmp_path,
        bucket="pairly-documents",
        signing_key=url_signing_key(SECRET),
        public_base_url="http://localhost:8000/",
    )


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize("env", [Environment.PRODUCTION, Environment.STAGING])
def test_filesystem_store_and_fake_scanner_are_refused_outside_dev(env: Environment) -> None:
    with pytest.raises(ConfigError, match="filesystem object store"):
        load_ingestion_settings(
            env=env,
            s3_bucket="b",
            s3_access_key="k",
            s3_secret_key=SECRET,
            object_store="filesystem",
            object_store_dir="/tmp/objects",  # noqa: S108 (never written)
        )
    with pytest.raises(ConfigError, match="dev-fake"):
        load_ingestion_settings(
            env=env,
            s3_bucket="b",
            s3_access_key="k",
            s3_secret_key=SECRET,
            virus_scanner="dev-fake",
        )


def test_filesystem_store_needs_a_folder() -> None:
    with pytest.raises(ConfigError, match="PAIRLY_OBJECT_STORE_DIR"):
        load_ingestion_settings(
            env=Environment.DEVELOPMENT,
            s3_bucket="b",
            s3_access_key="k",
            s3_secret_key=SECRET,
            object_store="filesystem",
        )


def test_factory_picks_the_store(tmp_path: Path) -> None:
    fs = object_store_from_settings(
        _ingestion(Environment.TEST, object_store="filesystem", object_store_dir=str(tmp_path))
    )
    assert isinstance(fs, FilesystemObjectStore)
    assert fs.bucket_dir == (tmp_path / "pairly-documents").resolve()
    s3 = object_store_from_settings(
        _ingestion(Environment.PRODUCTION, s3_endpoint_url="https://s3.example.test")
    )
    assert isinstance(s3, S3ObjectStore)


# ------------------------------------------------------------------ filesystem objects


async def test_round_trip_with_checksum(tmp_path: Path) -> None:
    store = _fs_store(tmp_path)
    data = b"%PDF-1.7 synthetic receipt"
    sha = hashlib.sha256(data).hexdigest()
    src = tmp_path / "upload"
    src.write_bytes(data)
    key = blob_key(ORG, sha)
    assert not await store.exists(key)
    await store.put_file(key, src, content_type="application/pdf", sha256=sha)
    assert await store.exists(key)
    assert await store.get_bytes(key, max_bytes=1024) == data
    out = tmp_path / "out"
    assert await store.download_to(key, out, max_bytes=1024) == len(data)
    assert out.read_bytes() == data
    assert await store.read_object(key, max_bytes=1024) == (data, "application/pdf")
    # No temp files are left behind.
    assert sorted(p.name for p in store._path(key).parent.iterdir()) == [sha, f"{sha}.meta.json"]


async def test_put_file_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    store = _fs_store(tmp_path)
    src = tmp_path / "upload"
    src.write_bytes(b"real bytes")
    key = blob_key(ORG, "0" * 64)
    with pytest.raises(StorageError, match="checksum mismatch"):
        await store.put_file(key, src, content_type="application/pdf", sha256="0" * 64)
    assert not await store.exists(key)
    assert list(store._path(key).parent.iterdir()) == []


async def test_reads_refuse_tampered_missing_and_oversized_objects(tmp_path: Path) -> None:
    store = _fs_store(tmp_path)
    key = derived_key(ORG, "a" * 64, "parsed.json")
    await store.put_bytes(key, b'{"pages": []}', content_type="application/json")
    with pytest.raises(StorageError, match="larger than allowed"):
        await store.get_bytes(key, max_bytes=4)
    store._path(key).write_bytes(b'{"pages": [1]}')  # same folder, different bytes
    with pytest.raises(StorageError):
        await store.get_bytes(key, max_bytes=1024)
    store._path(key).write_bytes(b'{"pages": {}}')  # same size, different bytes
    with pytest.raises(StorageError, match="SHA-256"):
        await store.get_bytes(key, max_bytes=1024)
    with pytest.raises(ObjectNotFoundError):
        await store.get_bytes(derived_key(ORG, "b" * 64, "parsed.json"), max_bytes=1024)


async def test_only_pairly_keys(tmp_path: Path) -> None:
    store = _fs_store(tmp_path)
    for key in ("../../etc/passwd", f"orgs/{ORG}/blobs/sha256/aa/../../x", "audit-anchors/v1/x"):
        with pytest.raises(ValueError, match="not a Pairly object key"):
            await store.put_bytes(key, b"x", content_type="text/plain")


def test_signed_urls(tmp_path: Path) -> None:
    store = _fs_store(tmp_path)
    key = blob_key(ORG, "c" * 64)
    url = store.presigned_get_url(
        key, expires_in=60, filename="Receipt é.pdf", content_type="application/pdf"
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == "http://localhost:8000"
    assert parts.path == f"{DEV_OBJECTS_PATH}/{key}"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["disposition"].startswith('attachment; filename="Receipt _.pdf"')
    good: dict[str, Any] = {
        "expires": int(q["expires"]),
        "content_type": q["type"],
        "disposition": q["disposition"],
        "sig": q["sig"],
    }
    assert store.verify_signature(key, **good)
    # Every signed part is bound: type, disposition, key, expiry.
    assert not store.verify_signature(key, **(good | {"content_type": "text/html"}))
    assert not store.verify_signature(key, **(good | {"disposition": "inline"}))
    assert not store.verify_signature(blob_key(ORG, "d" * 64), **good)
    assert not store.verify_signature(key, **(good | {"expires": good["expires"] + 1}))
    # Expired, or claiming a lifetime no signer issues.
    assert not store.verify_signature(key, **good, now=time.time() + 61)
    other = FilesystemObjectStore(
        root=tmp_path,
        bucket="pairly-documents",
        signing_key=url_signing_key("another-secret-of-enough-length"),
        public_base_url="http://localhost:8000",
    )
    assert not other.verify_signature(key, **good)
    with pytest.raises(ValueError, match="expires_in"):
        store.presigned_get_url(key, expires_in=3600, filename=None, content_type="x/y")


# ------------------------------------------------------------------ anchors


def test_anchor_settings_refuse_filesystem_outside_dev() -> None:
    common = {"s3_access_key": "k", "s3_secret_key": SECRET, "object_store": "filesystem"}
    with pytest.raises(ConfigError, match="development and test only"):
        load_anchor_settings(env=Environment.PRODUCTION, object_store_dir="/x", **common)
    with pytest.raises(ConfigError, match="PAIRLY_OBJECT_STORE_DIR"):
        load_anchor_settings(env=Environment.DEVELOPMENT, **common)


async def test_filesystem_anchors_are_write_once(tmp_path: Path) -> None:
    settings = load_anchor_settings(
        env=Environment.DEVELOPMENT,
        s3_access_key="k",
        s3_secret_key=SECRET,
        object_store="filesystem",
        object_store_dir=str(tmp_path),
    )
    store = anchor_store_from_settings(settings)
    assert isinstance(store, FilesystemAnchorStore)
    org = uuid.uuid4()
    first, second = anchor_key(org, 1), anchor_key(org, 2)
    assert await store.put_new(second, b'{"seq": 2}')
    assert await store.put_new(first, b'{"seq": 1}')
    # Never overwritten.
    assert not await store.put_new(first, b'{"seq": 1, "forged": true}')
    assert await store.get(first) == b'{"seq": 1}'
    assert await store.list_keys(org_prefix(org)) == [first, second]
    assert await store.list_keys(org_prefix(uuid.uuid4())) == []
    path = tmp_path / "pairly-audit-anchors" / first
    assert path.stat().st_mode & 0o222 == 0
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == []


# ------------------------------------------------------------------ outbox


def _email(to: str = "Ana <ana@example.test>") -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        sender="Pairly <no-reply@pairly.localhost>",
        subject="Approve: Invoice SYN-1",
        text="Open https://app.example.test/approvals/link#tok",
        html='<a href="https://app.example.test/approvals/link#tok">Approve</a>',
        locale="en",
        message_id="n-1@pairly.localhost",
        idempotency_key="n-1",
    )


def test_outbox_is_refused_outside_dev(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="development and test only"):
        load_notification_settings(
            env=Environment.PRODUCTION,
            email_transport="outbox",
            outbox_dir=str(tmp_path),
            public_web_url="https://app.example.test",
            public_api_url="https://api.example.test",
            signed_link_secret=SecretStr("x" * 40),
        )
    with pytest.raises(ConfigError, match="outbox_dir"):
        load_notification_settings(env=Environment.TEST, email_transport="outbox")


async def test_outbox_writes_eml_json_and_index(tmp_path: Path) -> None:
    settings = NotificationSettings(
        env=Environment.DEVELOPMENT,
        email_transport="outbox",
        outbox_dir=str(tmp_path),
        signed_link_secret=SecretStr("x" * 40),
    )
    transport = email_transport_from_settings(settings)
    assert isinstance(transport, OutboxTransport)
    assert await transport.send(_email()) == "<n-1@pairly.localhost>"
    await transport.send(_email("bo@example.test"))
    lines = (tmp_path / INDEX_FILE).read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    assert [e["to"] for e in entries] == [["ana@example.test"], ["bo@example.test"]]
    assert entries[0]["id"] < entries[1]["id"]
    full = json.loads((tmp_path / f"{entries[0]['id']}.json").read_text())
    assert full["v"] == 1
    assert full["subject"] == "Approve: Invoice SYN-1"
    assert "/approvals/link#tok" in full["html"]
    eml = message_from_bytes((tmp_path / entries[0]["eml"]).read_bytes())
    assert eml["To"] == "Ana <ana@example.test>"
    assert eml["Message-ID"] == "<n-1@pairly.localhost>"


# ------------------------------------------------------------------ scanner


async def test_dev_fake_scanner_is_labelled_and_catches_eicar(tmp_path: Path) -> None:
    scanner = scanner_from_settings(_ingestion(Environment.DEVELOPMENT, virus_scanner="dev-fake"))
    assert isinstance(scanner, FakeScanner)
    clean, infected = tmp_path / "clean", tmp_path / "eicar"
    clean.write_bytes(b"hello")
    infected.write_bytes(EICAR)
    ok = await scanner.scan_file(clean)
    bad = await scanner.scan_file(infected)
    assert (ok.verdict, ok.engine) == (Verdict.CLEAN, DEV_FAKE_ENGINE)
    assert "not a virus scan" in DEV_FAKE_ENGINE
    assert len(DEV_FAKE_ENGINE) <= 128  # documents.scan_engine
    assert (bad.verdict, bad.signature) == (Verdict.INFECTED, "Eicar-Test-Signature")


def test_scanner_factory_without_dev_fake() -> None:
    assert scanner_from_settings(_ingestion(Environment.PRODUCTION)) is None
    clamd = scanner_from_settings(_ingestion(Environment.PRODUCTION, clamav_host="clamav"))
    assert isinstance(clamd, ClamdScanner)

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Ingestion building blocks without a database: sniffing, names, spooling, limits,
storage keys and stores, the job queue and document permissions."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]

from pairly_core.config import ConfigError, Environment
from pairly_core.documents.permissions import authorize_document, document_permission
from pairly_core.ingestion import sniff as sn
from pairly_core.ingestion.queue import (
    ArqJobQueue,
    NullJobQueue,
    RecordingJobQueue,
    job_dumps,
    job_loads,
)
from pairly_core.ingestion.service import (
    SpoolWriter,
    TooLargeError,
    sanitize_filename,
    spool,
    spool_bytes,
)
from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy, load_ingestion_settings
from pairly_core.ingestion.storage import (
    InMemoryObjectStore,
    ObjectNotFoundError,
    S3ObjectStore,
    StorageError,
    blob_key,
    check_key,
    content_disposition,
    derived_key,
)
from pairly_core.rbac import (
    Actor,
    ActorKind,
    PermissionDeniedError,
    PermissionSpec,
    RoleGrant,
    build_registry,
)
from pairly_core.workflow.spec import GraphSpec

from . import doc_fixtures as fx

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
SHA = hashlib.sha256(b"synthetic").hexdigest()


def settings(**kw: Any) -> IngestionSettings:
    base: dict[str, Any] = {
        "env": Environment.TEST,
        "s3_bucket": "test-bucket",
        "s3_access_key": "test-access",
        "s3_secret_key": "test-secret-value",
    }
    base.update(kw)
    return IngestionSettings(**base)


# ------------------------------------------------------------------ sniffing


def _sniff_bytes(tmp_path: Path, data: bytes) -> str:
    path = tmp_path / "f"
    path.write_bytes(data)
    return sn.sniff(data[: sn.SNIFF_BYTES], path)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (fx.simple_pdf(), sn.PDF),
        (b"junk before header %PDF-1.7\n", sn.PDF),
        (fx.text_png(), sn.PNG),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, sn.JPEG),
        (b"II*\x00" + b"\x00" * 20, sn.TIFF),
        (b"MM\x00*" + b"\x00" * 20, sn.TIFF),
        (b"\xef\xbb\xbf  <?xml version='1.0'?><Invoice/>", sn.XML),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20, sn.OLE),
        (b"MZ\x90\x00 an executable", sn.OCTET),
        (b"plain text is never sniffed as text", sn.OCTET),
    ],
)
def test_sniff_signatures(tmp_path: Path, data: bytes, expected: str) -> None:
    assert _sniff_bytes(tmp_path, data) == expected


def test_sniff_office_packages(tmp_path: Path) -> None:
    assert _sniff_bytes(tmp_path, fx.tricky_docx()) == sn.DOCX
    # Macro-enabled documents and other ZIPs are not DOCX.
    assert _sniff_bytes(tmp_path, fx.docm_like()) == sn.ZIP
    assert _sniff_bytes(tmp_path, fx.plain_zip()) == sn.ZIP
    assert _sniff_bytes(tmp_path, b"PK\x03\x04 truncated") == sn.ZIP
    # Without a path a ZIP cannot be inspected.
    assert sn.sniff(fx.tricky_docx()[:64]) == sn.ZIP


@pytest.mark.parametrize(
    "part",
    ["word/vbaProject.bin", "word/vbaData.xml", "word/activeX/activeX1.xml", "VBAPROJECT.BIN"],
)
def test_a_docx_that_carries_macros_or_activex_is_not_a_docx(tmp_path: Path, part: str) -> None:
    """Security review 2026-10 (L-04): a package that keeps the plain DOCX content type
    but carries a VBA project or ActiveX parts is refused like a .docm."""
    assert _sniff_bytes(tmp_path, fx.docx_with_part(part)) == sn.ZIP
    # Other extra parts (an embedded image) leave it a DOCX.
    assert _sniff_bytes(tmp_path, fx.docx_with_part("word/media/extra.png")) == sn.DOCX


def test_a_docx_with_a_remote_template_or_external_part_is_not_a_docx(tmp_path: Path) -> None:
    """Security review 2026-10 (L-12): a relationship outside the package (a remote
    attached template, an external image, frame or OLE object) makes a plain DOCX a ZIP,
    which every intake policy refuses; ``active_content`` names why."""
    template = tmp_path / "template.docx"
    template.write_bytes(fx.docx_with_remote_template())
    assert sn.sniff(template.read_bytes()[: sn.SNIFF_BYTES], template) == sn.ZIP
    assert sn.active_content(template) == sn.EXTERNAL_TEMPLATE
    # Any case of the target mode, any relationships part.
    for name, rel in [
        ("word/_rels/header7.xml.rels", ("image", "https://x.example.invalid/p.png", "External")),
        ("word/_rels/footer7.xml.rels", ("oleObject", "file:///c:/x.bin", "external")),
        ("word/_rels/webSettings.xml.rels", ("frame", "https://x.example.invalid/", "EXTERNAL")),
        ("_rels/extra.rels", ("subDocument", "\\\\host\\share\\d.docx", "External")),
    ]:
        path = tmp_path / "external.docx"
        path.write_bytes(fx.docx_with_part(name, fx.rels_xml(rel)))
        assert sn.sniff(path.read_bytes()[: sn.SNIFF_BYTES], path) == sn.ZIP, name
        assert sn.active_content(path) == sn.EXTERNAL_RELATIONSHIP, name
    # A relationships part that cannot be read fails closed.
    broken = tmp_path / "broken.docx"
    broken.write_bytes(fx.docx_with_part("word/_rels/odd.xml.rels", b"<Relationships"))
    assert sn.active_content(broken) == sn.UNREADABLE_RELATIONSHIPS
    assert _sniff_bytes(tmp_path, broken.read_bytes()) == sn.ZIP
    # Macros keep their own name.
    macros = tmp_path / "macros.docx"
    macros.write_bytes(fx.docx_with_part("word/vbaProject.bin"))
    assert sn.active_content(macros) == sn.MACROS


def test_hyperlinks_and_internal_relationships_leave_a_docx(tmp_path: Path) -> None:
    """A hyperlink is the one external relationship kept (Word never follows it by
    itself; CVs carry profile links); targets inside the package are always fine."""
    path = tmp_path / "links.docx"
    path.write_bytes(
        fx.docx_with_part(
            "word/_rels/footnotes.xml.rels",
            fx.rels_xml(
                ("hyperlink", "https://www.example.com/in/someone", "External"),
                ("image", "media/image1.png", None),
                ("image", "media/image2.png", "Internal"),
            ),
        )
    )
    assert sn.sniff(path.read_bytes()[: sn.SNIFF_BYTES], path) == sn.DOCX
    assert sn.active_content(path) is None
    plain = tmp_path / "plain.docx"
    plain.write_bytes(fx.tricky_docx())
    assert sn.active_content(plain) is None
    zipped = tmp_path / "plain.zip"
    zipped.write_bytes(fx.plain_zip())
    assert sn.active_content(zipped) is None


def test_extension_for() -> None:
    assert sn.extension_for(sn.PDF) == ".pdf"
    assert sn.extension_for("application/x-unknown") == ".bin"


# ------------------------------------------------------------------ file names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\cv.docx", "cv.docx"),
        ("  .hidden.pdf ", "hidden.pdf"),
        ("in\u202evoice\u202cfdp.exe", "invoicefdp.exe"),
        ("a\x00b\nc.pdf", "abc.pdf"),
        ("", None),
        (None, None),
        ("...", None),
        ("/", None),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str | None) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_keeping_extension() -> None:
    name = sanitize_filename("\u0627" * 300 + ".pdf")
    assert name is not None
    assert name.endswith(".pdf")
    assert len(name.encode("utf-8")) <= 255
    long_no_ext = sanitize_filename("x" * 400)
    assert long_no_ext == "x" * 255


# ------------------------------------------------------------------ spooling


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_spool_hashes_while_streaming(tmp_path: Path) -> None:
    data = fx.simple_pdf()
    spooled = await spool(_chunks(data[:100], b"", data[100:]), tmp_path, max_bytes=len(data))
    assert spooled.sha256 == hashlib.sha256(data).hexdigest()
    assert spooled.size == len(data)
    assert spooled.path.read_bytes() == data
    assert spooled.head == data[: sn.SNIFF_BYTES]
    assert spooled.path.stat().st_mode & 0o777 == 0o600


async def test_spool_stops_early_and_cleans_up(tmp_path: Path) -> None:
    with pytest.raises(TooLargeError) as exc:
        await spool(_chunks(b"a" * 10, b"b" * 10, b"never read"), tmp_path, max_bytes=15)
    assert exc.value.limit == 15
    assert list(tmp_path.iterdir()) == []


async def test_spool_bytes_and_writer_abort(tmp_path: Path) -> None:
    spooled = await spool_bytes(b"x" * 600_000, tmp_path, max_bytes=1_000_000)
    assert spooled.size == 600_000
    writer = SpoolWriter(tmp_path, max_bytes=5)
    writer.write(b"abc")
    writer.abort()
    assert not writer.path.exists()


# ------------------------------------------------------------------ settings and limits


def test_policy_defaults_and_overrides() -> None:
    s = settings(
        intake_policies={"pay": {"allowed_types": [sn.PDF], "max_bytes": 1024}},
        intake_default_types="application/pdf, image/png",
    )
    assert s.policy_for("pay") == IntakePolicy(allowed_types=frozenset({sn.PDF}), max_bytes=1024)
    default = s.policy_for("hire")
    assert default.allowed_types == {sn.PDF, sn.PNG}
    assert default.max_bytes == s.intake_default_max_bytes


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "intake_policies": {
                "pay": {"allowed_types": ["application/x-msdownload"], "max_bytes": 1}
            }
        },
        {"intake_policies": {"pay": {"allowed_types": [], "max_bytes": 1}}},
        {"intake_policies": {"Bad-Id": {"allowed_types": [sn.PDF], "max_bytes": 1}}},
        {"intake_policies": {"pay": {"allowed_types": [sn.PDF], "max_bytes": 10**9}}},
        {"intake_default_max_bytes": 10**9},
        {"inbound_email_domain": "not a domain"},
        {"s3_endpoint_url": "ftp://minio:9000"},
        {"s3_endpoint_url": "http://minio:9000/bucket"},
        {"env": Environment.PRODUCTION, "s3_endpoint_url": "http://minio:9000"},
    ],
)
def test_invalid_settings(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_ingestion_settings(
            s3_bucket="b",
            s3_access_key="a",
            s3_secret_key="s",  # noqa: S106 (test value)
            **{"env": "test", **overrides},
        )


def test_missing_settings_named_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PAIRLY_S3_BUCKET", "PAIRLY_S3_ACCESS_KEY", "PAIRLY_S3_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as exc:
        load_ingestion_settings()
    message = str(exc.value)
    assert "PAIRLY_S3_BUCKET" in message
    assert "PAIRLY_S3_SECRET_KEY" in message


def test_endpoint_normalised() -> None:
    s = settings(s3_endpoint_url="http://minio:9000/", s3_public_endpoint_url=" ")
    assert s.s3_endpoint_url == "http://minio:9000"
    assert s.s3_public_endpoint_url is None


# ------------------------------------------------------------------ storage


def test_keys_are_built_from_org_and_hash_only() -> None:
    assert blob_key(ORG, SHA) == f"orgs/{ORG}/blobs/sha256/{SHA[:2]}/{SHA}"
    assert derived_key(ORG, SHA, "normalized-pdf-1.json").endswith("/normalized-pdf-1.json")
    for bad in (lambda: blob_key(ORG, "../x"), lambda: blob_key("org", SHA)):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="blob keys"):
            bad()
    with pytest.raises(ValueError, match="artifact"):
        derived_key(ORG, SHA, "../../escape")
    with pytest.raises(ValueError, match="derived keys"):
        derived_key(ORG, "nothex", "a.json")
    with pytest.raises(ValueError, match="not a Pairly object key"):
        check_key("orgs/../other")


def test_content_disposition_is_safe() -> None:
    header = content_disposition('in"voice\\\u0641.pdf', "document")
    assert header.startswith('attachment; filename="in_voice__.pdf"')
    assert "filename*=UTF-8''in%22voice%5C%D9%81.pdf" in header
    assert content_disposition(None, "document").startswith('attachment; filename="document"')


async def test_in_memory_store(tmp_path: Path) -> None:
    store = InMemoryObjectStore()
    path = tmp_path / "f"
    path.write_bytes(b"synthetic")
    key = blob_key(ORG, SHA)
    await store.put_file(key, path, content_type="text/plain", sha256=SHA)
    assert await store.exists(key)
    assert await store.get_bytes(key, max_bytes=100) == b"synthetic"
    out = tmp_path / "out"
    assert await store.download_to(key, out, max_bytes=100) == 9
    with pytest.raises(StorageError):
        await store.get_bytes(key, max_bytes=3)
    with pytest.raises(StorageError, match="checksum"):
        await store.put_file(key, path, content_type="text/plain", sha256="0" * 64)
    with pytest.raises(ObjectNotFoundError):
        await store.get_bytes(blob_key(ORG, "a" * 64), max_bytes=10)
    url = store.presigned_get_url(key, expires_in=60, filename="cv.pdf", content_type="x/y")
    assert store.verify_url(url)
    assert not store.verify_url(url, now=time.time() + 61)
    tampered = url.replace(SHA, "b" * 64)
    assert not store.verify_url(tampered)


def _s3_store() -> tuple[S3ObjectStore, Stubber]:
    s = settings(
        s3_endpoint_url="http://minio:9000",
        s3_public_endpoint_url="http://localhost:9000",
    )
    store = S3ObjectStore.from_settings(s)
    stubber = Stubber(store._client)
    return store, stubber


async def test_s3_put_sends_checksum(tmp_path: Path) -> None:
    store, stubber = _s3_store()
    path = tmp_path / "f"
    path.write_bytes(b"synthetic")
    key = blob_key(ORG, SHA)
    import base64

    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "test-bucket",
            "Key": key,
            "Body": ANY,
            "ContentType": "application/pdf",
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(SHA)).decode(),
        },
    )
    stubber.add_client_error("put_object", service_error_code="BadDigest", http_status_code=400)
    with stubber:
        await store.put_file(key, path, content_type="application/pdf", sha256=SHA)
        with pytest.raises(StorageError):
            await store.put_file(key, path, content_type="application/pdf", sha256=SHA)
    stubber.assert_no_pending_responses()


async def test_s3_exists_get_and_limits(tmp_path: Path) -> None:
    store, stubber = _s3_store()
    key = blob_key(ORG, SHA)
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stubber.add_response("head_object", {"ContentLength": 9})
    stubber.add_response("head_object", {"ContentLength": 9})
    stubber.add_client_error(
        "head_object", service_error_code="InternalError", http_status_code=500
    )
    with stubber:
        assert await store.exists(key) is False
        assert await store.exists(key) is True
        with pytest.raises(StorageError, match="larger"):
            await store.get_bytes(key, max_bytes=3)
        with pytest.raises(StorageError, match="head_object failed"):
            await store.download_to(key, tmp_path / "x", max_bytes=100)


async def test_s3_get_and_download() -> None:
    import io

    from botocore.response import StreamingBody  # type: ignore[import-untyped]

    store, stubber = _s3_store()
    key = blob_key(ORG, SHA)

    def body() -> StreamingBody:
        return StreamingBody(io.BytesIO(b"synthetic"), 9)

    stubber.add_response("head_object", {"ContentLength": 9})
    stubber.add_response("get_object", {"Body": body()})
    stubber.add_response("put_object", {})
    with stubber:
        assert await store.get_bytes(key, max_bytes=100) == b"synthetic"
        await store.put_bytes(derived_key(ORG, SHA, "a.json"), b"{}", content_type="x/y")


async def test_s3_download_to(tmp_path: Path) -> None:
    import io

    from botocore.response import StreamingBody

    store, stubber = _s3_store()
    key = blob_key(ORG, SHA)
    stubber.add_response("head_object", {"ContentLength": 9})
    stubber.add_response("get_object", {"Body": StreamingBody(io.BytesIO(b"synthetic"), 9)})
    with stubber:
        assert await store.download_to(key, tmp_path / "out", max_bytes=100) == 9
    assert (tmp_path / "out").read_bytes() == b"synthetic"


def test_s3_presigned_url_uses_public_endpoint() -> None:
    store, _ = _s3_store()
    key = blob_key(ORG, SHA)
    url = store.presigned_get_url(key, expires_in=60, filename="cv.pdf", content_type=sn.PDF)
    parts = urlsplit(url)
    assert parts.netloc == "localhost:9000"
    assert parts.path == f"/test-bucket/{key}"
    query = parse_qs(parts.query)
    assert query["X-Amz-Expires"] == ["60"]
    assert query["response-content-type"] == [sn.PDF]
    assert query["response-content-disposition"][0].startswith("attachment;")
    assert "test-secret-value" not in url


# ------------------------------------------------------------------ queue


class _Pool:
    def __init__(self, result: Any = "queued", error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed = True


async def test_job_queues() -> None:
    doc = uuid.uuid4()
    pool = _Pool()
    queue = ArqJobQueue(pool)
    assert await queue.enqueue("scan_document", org_id=ORG, document_id=doc, key="0")
    args, kwargs = pool.calls[0]
    assert args == ("scan_document", str(ORG), str(doc))
    assert kwargs == {"_job_id": f"scan_document:{doc}:0"}
    assert not await ArqJobQueue(_Pool(result=None)).enqueue(
        "scan_document", org_id=ORG, document_id=doc, key="0"
    )
    assert not await ArqJobQueue(_Pool(error=ConnectionError())).enqueue(
        "scan_document", org_id=ORG, document_id=doc, key="0"
    )
    await queue.aclose()
    assert pool.closed
    assert not await NullJobQueue().enqueue("x", org_id=ORG, document_id=doc, key="0")
    recording = RecordingJobQueue()
    assert await recording.enqueue("x", org_id=ORG, document_id=doc, key="1")
    assert recording.jobs == [("x", ORG, doc, "1")]
    assert job_loads(job_dumps({"a": [1, "b"]})) == {"a": [1, "b"]}


# ------------------------------------------------------------------ permissions


class _Toy:
    id = "toy"
    name = "Toy"
    permissions = (
        PermissionSpec("toy.document.read", "Read toy documents", frozenset({"admin"})),
        PermissionSpec("toy.document.upload", "Upload toy documents", frozenset({"admin"})),
    )
    graphs: tuple[GraphSpec, ...] = ()


class _Bare:
    id = "bare"
    name = "Bare"
    permissions = (PermissionSpec("bare.item.read", "Read items", frozenset({"admin"})),)
    graphs: tuple[GraphSpec, ...] = ()


def _actor(
    registry: Any, role: str, workflow: str | None = None, kind: ActorKind = ActorKind.USER
) -> Actor:
    grants = (RoleGrant(role, workflow),)
    return Actor(
        kind=kind,
        id=uuid.uuid4(),
        org_id=ORG,
        grants=grants,
        permissions=registry.resolve(grants),
    )


def test_document_permissions_registered_and_routed() -> None:
    registry = build_registry([_Toy(), _Bare()])
    assert "core.document.read" in registry
    assert "core.document.upload" in registry
    assert document_permission(registry, "toy", "upload") == "toy.document.upload"
    assert document_permission(registry, "bare", "upload") == "core.document.upload"
    with pytest.raises(ValueError, match="unknown document action"):
        document_permission(registry, "bare", "delete")


def test_document_authorisation_rules() -> None:
    registry = build_registry([_Toy(), _Bare()])
    clerk = _actor(registry, "ap_clerk")
    authorize_document(clerk, registry, workflow="bare", action="upload")
    # The toy workflow owns its document permissions: only admins.
    with pytest.raises(PermissionDeniedError):
        authorize_document(clerk, registry, workflow="toy", action="read")
    authorize_document(_actor(registry, "admin"), registry, workflow="toy", action="upload")
    # Workflow-scoped members get no core.* permission.
    scoped = _actor(registry, "ap_clerk", workflow="bare")
    with pytest.raises(PermissionDeniedError):
        authorize_document(scoped, registry, workflow="bare", action="upload")
    # Agents read but never upload.
    agent = _actor(registry, "agent", kind=ActorKind.AGENT)
    authorize_document(agent, registry, workflow="bare", action="read")
    with pytest.raises(PermissionDeniedError):
        authorize_document(agent, registry, workflow="bare", action="upload")
    # Approvers read but do not upload.
    approver = _actor(registry, "approver")
    authorize_document(approver, registry, workflow="bare", action="read")
    with pytest.raises(PermissionDeniedError):
        authorize_document(approver, registry, workflow="bare", action="upload")

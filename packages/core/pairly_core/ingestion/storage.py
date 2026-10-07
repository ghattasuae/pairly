# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Object storage behind a small interface: S3 compatible (MinIO locally) or in memory.

Keys are built here only, from the organisation id and the content hash. User-supplied
names never reach a key, so a file name cannot traverse paths, collide with another
tenant's object or leak into storage logs:

    orgs/<org uuid>/blobs/sha256/<first two hex>/<sha256>          original bytes
    orgs/<org uuid>/derived/<sha256>/<artifact name>                derived artifacts
    orgs/<org uuid>/compliance/<export uuid>.zip                    compliance packs

Signed GET URLs are short lived and are only created by callers that already passed an
RBAC check (see ``pairly_api.routes.documents``). The download name and type in the URL
come from the sanitised display name and the sniffed type, and the object is always
served as an attachment.

boto3 is synchronous; calls run in a worker thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import quote, urlencode

from pairly_core.ingestion.settings import IngestionSettings

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_KEY_RE = re.compile(
    r"^orgs/[0-9a-f-]{36}/"
    r"(blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}|derived/[0-9a-f]{64}/[a-z0-9][a-z0-9._-]{0,127}"
    # Compliance packs (pairly_core.decisions.compliance).
    r"|compliance/[0-9a-f-]{36}\.zip)$"
)


class StorageError(RuntimeError):
    """The object store failed or refused an operation."""


class ObjectNotFoundError(StorageError):
    """No object under that key."""


def blob_key(org_id: uuid.UUID, sha256: str) -> str:
    """Key of a document's original bytes."""
    if not isinstance(org_id, uuid.UUID) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("blob keys need an org UUID and a lower-case hex SHA-256")
    return f"orgs/{org_id}/blobs/sha256/{sha256[:2]}/{sha256}"


def derived_key(org_id: uuid.UUID, sha256: str, artifact: str) -> str:
    """Key of an artifact derived from a document (for example the parsed JSON)."""
    if not isinstance(org_id, uuid.UUID) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("derived keys need an org UUID and a lower-case hex SHA-256")
    if not _ARTIFACT_RE.fullmatch(artifact):
        raise ValueError("invalid artifact name")
    return f"orgs/{org_id}/derived/{sha256}/{artifact}"


def check_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError("not a Pairly object key")
    return key


def content_disposition(filename: str | None, fallback: str) -> str:
    """``attachment`` with an ASCII fallback and an RFC 5987 UTF-8 name."""
    name = filename or fallback
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@runtime_checkable
class ObjectStore(Protocol):
    async def put_file(self, key: str, path: Path, *, content_type: str, sha256: str) -> None:
        """Store the file at ``path``. ``sha256`` is verified by the store when it can."""
        ...

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def download_to(self, key: str, path: Path, *, max_bytes: int) -> int:
        """Write the object to ``path``; refuse objects above ``max_bytes``. Returns size."""
        ...

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes: ...

    def presigned_get_url(
        self, key: str, *, expires_in: int, filename: str | None, content_type: str
    ) -> str:
        """A short-lived GET URL. Call only after an RBAC check."""
        ...


# ------------------------------------------------------------------ in memory (tests)


@dataclass
class StoredObject:
    data: bytes
    content_type: str


@dataclass
class InMemoryObjectStore:
    """Process-local store for tests and examples. Signed URLs are HMAC tokens."""

    objects: dict[str, StoredObject] = field(default_factory=dict)
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    puts: int = 0

    async def put_file(self, key: str, path: Path, *, content_type: str, sha256: str) -> None:
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError("checksum mismatch")
        await self.put_bytes(key, data, content_type=content_type)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        check_key(key)
        self.objects[key] = StoredObject(bytes(data), content_type)
        self.puts += 1

    async def exists(self, key: str) -> bool:
        return check_key(key) in self.objects

    async def download_to(self, key: str, path: Path, *, max_bytes: int) -> int:
        data = await self.get_bytes(key, max_bytes=max_bytes)
        await asyncio.to_thread(path.write_bytes, data)
        return len(data)

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        obj = self.objects.get(check_key(key))
        if obj is None:
            raise ObjectNotFoundError(key)
        if len(obj.data) > max_bytes:
            raise StorageError("object larger than allowed")
        return obj.data

    def presigned_get_url(
        self, key: str, *, expires_in: int, filename: str | None, content_type: str
    ) -> str:
        check_key(key)
        expires = int(time.time()) + expires_in
        params = {
            "expires": str(expires),
            "type": content_type,
            "disposition": content_disposition(filename, "document"),
        }
        message = f"{key}\n{expires}".encode()
        params["sig"] = hmac.new(self.signing_key, message, hashlib.sha256).hexdigest()
        return f"memory://objects/{key}?{urlencode(params)}"

    def verify_url(self, url: str, *, now: float | None = None) -> bool:
        """Check a URL made by ``presigned_get_url`` (tests only)."""
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(url)
        key = parts.path.lstrip("/")
        query = parse_qs(parts.query)
        expires = int(query["expires"][0])
        expected = hmac.new(
            self.signing_key, f"{key}\n{expires}".encode(), hashlib.sha256
        ).hexdigest()
        current = time.time() if now is None else now
        return hmac.compare_digest(expected, query["sig"][0]) and current <= expires


# ------------------------------------------------------------------ S3 / MinIO


def _b64_sha256(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


class S3ObjectStore:
    """S3 compatible store (AWS S3 or MinIO) with path-style addressing."""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any,
        presign_client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self._client = client
        self._presign = presign_client or client

    @classmethod
    def from_settings(cls, settings: IngestionSettings) -> S3ObjectStore:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        common: Mapping[str, Any] = {
            "region_name": settings.s3_region,
            "aws_access_key_id": settings.s3_access_key.get_secret_value(),
            "aws_secret_access_key": settings.s3_secret_key.get_secret_value(),
            "config": config,
        }
        client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url, **common)
        presign = boto3.client(
            "s3",
            endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url,
            **common,
        )
        return cls(bucket=settings.s3_bucket, client=client, presign_client=presign)

    async def _call(self, method: str, **kwargs: Any) -> Any:
        fn = getattr(self._client, method)
        try:
            return await asyncio.to_thread(fn, Bucket=self.bucket, **kwargs)
        except Exception as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(kwargs.get("Key", "")) from None
            raise StorageError(f"object store {method} failed: {type(exc).__name__}") from exc

    async def put_file(self, key: str, path: Path, *, content_type: str, sha256: str) -> None:
        check_key(key)

        def _put() -> None:
            with path.open("rb") as body:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    # The store recomputes SHA-256 and refuses the write on a mismatch.
                    ChecksumSHA256=_b64_sha256(sha256),
                )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            raise StorageError(f"object store put failed: {type(exc).__name__}") from exc

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        check_key(key)
        digest = hashlib.sha256(data).hexdigest()
        await self._call(
            "put_object",
            Key=key,
            Body=data,
            ContentType=content_type,
            ChecksumSHA256=_b64_sha256(digest),
        )

    async def exists(self, key: str) -> bool:
        check_key(key)
        try:
            await self._call("head_object", Key=key)
        except ObjectNotFoundError:
            return False
        return True

    async def _head_size(self, key: str) -> int:
        head = await self._call("head_object", Key=key)
        return int(cast(Mapping[str, Any], head)["ContentLength"])

    async def download_to(self, key: str, path: Path, *, max_bytes: int) -> int:
        check_key(key)
        size = await self._head_size(key)
        if size > max_bytes:
            raise StorageError("object larger than allowed")
        response = await self._call("get_object", Key=key)
        stream = cast(Mapping[str, Any], response)["Body"]

        def _copy() -> int:
            written = 0
            with path.open("wb") as out:
                while chunk := stream.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise StorageError("object larger than allowed")
                    out.write(chunk)
            stream.close()
            return written

        return await asyncio.to_thread(_copy)

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        check_key(key)
        size = await self._head_size(key)
        if size > max_bytes:
            raise StorageError("object larger than allowed")
        response = await self._call("get_object", Key=key)
        stream = cast(Mapping[str, Any], response)["Body"]
        data = await asyncio.to_thread(stream.read, max_bytes + 1)
        if not isinstance(data, bytes) or len(data) > max_bytes:
            raise StorageError("object larger than allowed")
        return data

    def presigned_get_url(
        self, key: str, *, expires_in: int, filename: str | None, content_type: str
    ) -> str:
        check_key(key)
        url = self._presign.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": content_disposition(filename, "document"),
                "ResponseCacheControl": "private, no-store",
            },
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )
        return str(url)


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    return code in {"404", "NoSuchKey", "NotFound"}

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Filesystem object store for native development and tests (no MinIO).

Selected with ``PAIRLY_OBJECT_STORE=filesystem``; the settings refuse it outside the
development and test environments, so production always uses S3.

Layout: ``<root>/<bucket>/<key>`` for the bytes and ``<key>.meta.json`` next to them
(SHA-256, size and media type). Keys are checked by ``storage.check_key`` (the same
pattern the S3 store enforces), so no user input reaches a path, and every path is
checked again to resolve inside the bucket folder.

Like the S3 store:

* ``put_file`` refuses the write when the bytes do not hash to the given SHA-256
  (S3 does this with ``ChecksumSHA256``). Every read hashes the bytes again and refuses
  an object that no longer matches its recorded SHA-256.
* Writes are atomic (temp file, then rename), so a reader never sees half an object.
* Signed GET URLs are short lived. They point at the API's development route
  (``/v1/_dev/objects/<key>``, mounted only when this store is configured) and carry an
  HMAC over the key, the expiry, the media type and the content disposition, so none of
  them can be changed. The HMAC key is derived from ``PAIRLY_S3_SECRET_KEY``, the store's
  secret, just as S3 signs presigned URLs with its secret key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from pairly_core.ingestion.storage import (
    ObjectNotFoundError,
    StorageError,
    check_key,
    content_disposition,
)

DEV_OBJECTS_PATH: Final = "/v1/_dev/objects"
# Longest lifetime a signed URL may claim (the settings cap the TTL at 900 seconds).
MAX_URL_TTL_SECONDS: Final = 900
_URL_KEY_CONTEXT: Final = b"pairly-dev-object-url-v1"
_META_SUFFIX: Final = ".meta.json"
_CHUNK: Final = 1024 * 1024


def url_signing_key(secret: str) -> bytes:
    """The HMAC key for signed URLs, derived from the store secret (domain separated)."""
    if len(secret) < 16:
        raise ValueError("the object store secret must be at least 16 characters")
    return hmac.new(secret.encode("utf-8"), _URL_KEY_CONTEXT, hashlib.sha256).digest()


def _signature(
    key: bytes, object_key: str, expires: int, content_type: str, disposition: str
) -> str:
    message = "\n".join([object_key, str(expires), content_type, disposition]).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class FilesystemObjectStore:
    """``ObjectStore`` on the local filesystem. Development and test only."""

    def __init__(
        self,
        *,
        root: Path,
        bucket: str,
        signing_key: bytes,
        public_base_url: str,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("the URL signing key must be at least 32 bytes")
        self.bucket_dir = (Path(root) / bucket).resolve()
        self._signing_key = signing_key
        self.public_base_url = public_base_url.rstrip("/")

    # ---------------------------------------------------------------- paths

    def _path(self, key: str) -> Path:
        check_key(key)
        path = (self.bucket_dir / key).resolve()
        if not path.is_relative_to(self.bucket_dir):
            raise ValueError("not a Pairly object key")
        return path

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_name(path.name + _META_SUFFIX)

    # ---------------------------------------------------------------- writes

    def _write_sync(
        self, key: str, source: Path | bytes, content_type: str, sha256: str | None
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("xb") as out:
                if isinstance(source, bytes):
                    out.write(source)
                    digest.update(source)
                    size = len(source)
                else:
                    with source.open("rb") as handle:
                        while chunk := handle.read(_CHUNK):
                            out.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
            actual = digest.hexdigest()
            if sha256 is not None and not hmac.compare_digest(actual, sha256):
                raise StorageError("checksum mismatch")
            meta = {"sha256": actual, "size": size, "content_type": content_type}
            meta_tmp = tmp.with_name(tmp.name + _META_SUFFIX)
            meta_tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(meta_tmp, self._meta_path(path))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def put_file(self, key: str, path: Path, *, content_type: str, sha256: str) -> None:
        try:
            await asyncio.to_thread(self._write_sync, key, path, content_type, sha256)
        except (StorageError, ValueError):
            raise
        except OSError as exc:
            raise StorageError(f"object store put failed: {type(exc).__name__}") from exc

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        try:
            await asyncio.to_thread(self._write_sync, key, bytes(data), content_type, None)
        except (StorageError, ValueError):
            raise
        except OSError as exc:
            raise StorageError(f"object store put failed: {type(exc).__name__}") from exc

    # ---------------------------------------------------------------- reads

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    def _checked(self, key: str, max_bytes: int) -> tuple[Path, dict[str, object]]:
        """The object's path and metadata, after checking its size."""
        path = self._path(key)
        try:
            meta = json.loads(self._meta_path(path).read_text(encoding="utf-8"))
            size = path.stat().st_size
        except FileNotFoundError:
            raise ObjectNotFoundError(key) from None
        except (OSError, ValueError) as exc:
            raise StorageError(f"object store read failed: {type(exc).__name__}") from exc
        if not isinstance(meta, dict) or meta.get("size") != size:
            raise StorageError("object does not match its recorded size")
        if size > max_bytes:
            raise StorageError("object larger than allowed")
        return path, meta

    def _read_sync(self, key: str, max_bytes: int) -> tuple[bytes, str]:
        path, meta = self._checked(key, max_bytes)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFoundError(key) from None
        except OSError as exc:
            raise StorageError(f"object store read failed: {type(exc).__name__}") from exc
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(meta.get("sha256", ""))):
            raise StorageError("object does not match its recorded SHA-256")
        return data, str(meta.get("content_type", "application/octet-stream"))

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        data, _ = await asyncio.to_thread(self._read_sync, key, max_bytes)
        return data

    async def download_to(self, key: str, path: Path, *, max_bytes: int) -> int:
        data = await self.get_bytes(key, max_bytes=max_bytes)
        await asyncio.to_thread(path.write_bytes, data)
        return len(data)

    def _delete_sync(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        self._meta_path(path).unlink(missing_ok=True)

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._delete_sync, key)
        except ValueError:
            raise
        except OSError as exc:
            raise StorageError(f"object store delete failed: {type(exc).__name__}") from exc

    async def read_object(self, key: str, *, max_bytes: int) -> tuple[bytes, str]:
        """Verified bytes and the stored media type (for the development route)."""
        return await asyncio.to_thread(self._read_sync, key, max_bytes)

    # ---------------------------------------------------------------- signed URLs

    def presigned_get_url(
        self, key: str, *, expires_in: int, filename: str | None, content_type: str
    ) -> str:
        check_key(key)
        if not 1 <= expires_in <= MAX_URL_TTL_SECONDS:
            raise ValueError(f"expires_in must be between 1 and {MAX_URL_TTL_SECONDS} seconds")
        expires = int(time.time()) + expires_in
        disposition = content_disposition(filename, "document")
        params = {
            "expires": str(expires),
            "type": content_type,
            "disposition": disposition,
            "sig": _signature(self._signing_key, key, expires, content_type, disposition),
        }
        return f"{self.public_base_url}{DEV_OBJECTS_PATH}/{key}?{urlencode(params)}"

    def verify_signature(
        self,
        key: str,
        *,
        expires: int,
        content_type: str,
        disposition: str,
        sig: str,
        now: float | None = None,
    ) -> bool:
        """True when the URL parameters were signed by this store and have not expired."""
        try:
            check_key(key)
        except ValueError:
            return False
        current = time.time() if now is None else now
        if expires < current or expires > current + MAX_URL_TTL_SECONDS + 5:
            return False
        expected = _signature(self._signing_key, key, expires, content_type, disposition)
        return hmac.compare_digest(expected, sig)

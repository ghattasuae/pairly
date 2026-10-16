# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Write-once storage for signed audit chain heads, outside the database.

Keys: ``audit-anchors/v1/<org uuid>/<seq, 20 digits>.json``. One object per anchored
head; listing a prefix returns them in ``seq`` order.

Write once, three layers (the S3 store):

1. Every write is a conditional put (``If-None-Match: *``): an existing key is never
   overwritten by Pairly.
2. The bucket (``PAIRLY_AUDIT_ANCHOR_BUCKET``) is created with S3 Object Lock, and every
   object gets a retention date (``PAIRLY_AUDIT_ANCHOR_RETENTION_DAYS``) in the lock
   mode ``PAIRLY_AUDIT_ANCHOR_LOCK_MODE`` (``GOVERNANCE`` locally, ``COMPLIANCE`` for
   production; locked versions cannot be deleted or overwritten until they expire).
3. The application's storage key may get, list and put in that bucket, never delete
   (``infra/minio/init.sh``).

With ``PAIRLY_AUDIT_ANCHOR_LOCK_MODE=none`` (a store without object lock) only layers 1
and 3 apply: someone holding the storage root credentials could still delete anchors.
That is the documented limitation of such a deployment.

Native development and tests (``PAIRLY_OBJECT_STORE=filesystem``, refused elsewhere)
use ``FilesystemAnchorStore``: anchors are files under
``<PAIRLY_OBJECT_STORE_DIR>/<anchor bucket>/``, and write once is enforced in code. A new
anchor is written to a temp file and then hard-linked to its key, which fails when the
key exists, so an anchor is never overwritten or seen half written; the store has no
delete, and the files are made read-only.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pairly_core.config import ENV_PREFIX, ConfigError, Environment

PREFIX: Final = "audit-anchors/v1"
MAX_ANCHOR_BYTES: Final = 4096
_KEY_RE = re.compile(r"^audit-anchors/v1/[0-9a-f-]{36}/[0-9]{20}\.json$")


class AnchorStoreError(RuntimeError):
    """The anchor store failed or refused an operation."""


def anchor_key(org_id: uuid.UUID, seq: int) -> str:
    if not isinstance(org_id, uuid.UUID) or seq < 1:
        raise ValueError("anchor keys need an org UUID and seq >= 1")
    return f"{PREFIX}/{org_id}/{seq:020d}.json"


def org_prefix(org_id: uuid.UUID) -> str:
    return f"{PREFIX}/{org_id}/"


def check_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError("not an anchor key")
    return key


@runtime_checkable
class AnchorStore(Protocol):
    async def put_new(self, key: str, data: bytes) -> bool:
        """Store ``data`` under a new ``key``. False when the key already exists."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def list_keys(self, prefix: str) -> list[str]:
        """Keys under ``prefix``, sorted."""
        ...


@dataclass
class InMemoryAnchorStore:
    """Process-local store for tests. Write once, like the real one."""

    objects: dict[str, bytes] = field(default_factory=dict)

    async def put_new(self, key: str, data: bytes) -> bool:
        check_key(key)
        if key in self.objects:
            return False
        self.objects[key] = bytes(data)
        return True

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[check_key(key)]
        except KeyError:
            raise AnchorStoreError("anchor not found") from None

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


class AnchorSettings(BaseSettings):
    """Where anchors go. S3 endpoint and credentials are the documents' ones."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra="ignore", frozen=True
    )

    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    audit_anchor_bucket: str = Field(
        default="pairly-audit-anchors", pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
    )
    audit_anchor_lock_mode: str = Field(
        default="GOVERNANCE", pattern=r"^(GOVERNANCE|COMPLIANCE|none)$"
    )
    audit_anchor_retention_days: int = Field(default=2555, ge=1, le=36500)
    # Native development and tests only: anchors as write-once files.
    env: Environment = Environment.PRODUCTION
    object_store: Literal["s3", "filesystem"] = "s3"
    object_store_dir: str | None = None

    @model_validator(mode="after")
    def _filesystem_only_locally(self) -> AnchorSettings:
        if self.object_store == "filesystem":
            if self.env not in {Environment.DEVELOPMENT, Environment.TEST}:
                raise ValueError("the filesystem object store is for development and test only")
            if not (self.object_store_dir or "").strip():
                raise ValueError("the filesystem object store needs PAIRLY_OBJECT_STORE_DIR")
        return self


def load_anchor_settings(**overrides: Any) -> AnchorSettings:
    try:
        return AnchorSettings(**overrides)
    except ValidationError as exc:
        names = sorted(
            {f"{ENV_PREFIX}{str(e['loc'][0]).upper()}" for e in exc.errors() if e["loc"]}
        )
        # Whole-model checks (the filesystem store) carry no field; their messages
        # name settings, never values.
        names += sorted({str(e["msg"]) for e in exc.errors() if not e["loc"]})
        raise ConfigError(
            "invalid or missing anchor storage settings: " + ", ".join(names)
        ) from None


def anchor_store_from_settings(settings: AnchorSettings) -> AnchorStore:
    """S3 with object lock, or (development and test only) write-once files."""
    if settings.object_store == "filesystem":
        if settings.object_store_dir is None:  # pragma: no cover - checked by the settings
            raise ConfigError("missing required settings: PAIRLY_OBJECT_STORE_DIR")
        return FilesystemAnchorStore(Path(settings.object_store_dir) / settings.audit_anchor_bucket)
    return S3AnchorStore.from_settings(settings)


class FilesystemAnchorStore:
    """Anchors as files, write once enforced in code. Development and test only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("not an anchor key")
        return path

    def _put_sync(self, key: str, data: bytes) -> bool:
        if len(data) > MAX_ANCHOR_BYTES:
            raise AnchorStoreError("anchor object too large")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with tmp.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            tmp.chmod(0o444)
            try:
                # Atomic and exclusive: fails when the key exists (never overwrites).
                os.link(tmp, path)
            except FileExistsError:
                return False
            return True
        finally:
            tmp.unlink(missing_ok=True)

    async def put_new(self, key: str, data: bytes) -> bool:
        try:
            return await asyncio.to_thread(self._put_sync, key, bytes(data))
        except OSError as exc:
            raise AnchorStoreError(f"anchor put failed: {type(exc).__name__}") from exc

    def _get_sync(self, key: str) -> bytes:
        with self._path(key).open("rb") as handle:
            data = handle.read(MAX_ANCHOR_BYTES + 1)
        if len(data) > MAX_ANCHOR_BYTES:
            raise AnchorStoreError("anchor object too large")
        return data

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._get_sync, key)
        except FileNotFoundError:
            raise AnchorStoreError("anchor not found") from None
        except OSError as exc:
            raise AnchorStoreError(f"anchor get failed: {type(exc).__name__}") from exc

    def _list_sync(self, prefix: str) -> list[str]:
        keys: list[str] = []
        if not self.root.is_dir():
            return keys
        for path in self.root.rglob("*.json"):
            key = path.relative_to(self.root).as_posix()
            if _KEY_RE.fullmatch(key) and key.startswith(prefix):
                keys.append(key)
        return sorted(keys)

    async def list_keys(self, prefix: str) -> list[str]:
        try:
            return await asyncio.to_thread(self._list_sync, prefix)
        except OSError as exc:
            raise AnchorStoreError(f"anchor list failed: {type(exc).__name__}") from exc


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return ""
    error = response.get("Error", {})
    return str(error.get("Code", "")) if isinstance(error, Mapping) else ""


class S3AnchorStore:
    """Anchors in an S3 bucket with object lock (MinIO locally)."""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any,
        lock_mode: str = "GOVERNANCE",
        retention: timedelta = timedelta(days=2555),
    ) -> None:
        self.bucket = bucket
        self._client = client
        self.lock_mode = lock_mode
        self.retention = retention

    @classmethod
    def from_settings(cls, settings: AnchorSettings) -> S3AnchorStore:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        return cls(
            bucket=settings.audit_anchor_bucket,
            client=client,
            lock_mode=settings.audit_anchor_lock_mode,
            retention=timedelta(days=settings.audit_anchor_retention_days),
        )

    async def put_new(self, key: str, data: bytes) -> bool:
        check_key(key)
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": "application/json",
            "IfNoneMatch": "*",
        }
        if self.lock_mode != "none":
            params["ObjectLockMode"] = self.lock_mode
            params["ObjectLockRetainUntilDate"] = datetime.now(UTC) + self.retention
        try:
            await asyncio.to_thread(self._client.put_object, **params)
        except Exception as exc:
            if _error_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
                return False
            raise AnchorStoreError(f"anchor put failed: {type(exc).__name__}") from exc
        return True

    async def get(self, key: str) -> bytes:
        check_key(key)
        try:
            response = await asyncio.to_thread(self._client.get_object, Bucket=self.bucket, Key=key)
            body = cast(Mapping[str, Any], response)["Body"]
            data = await asyncio.to_thread(body.read, MAX_ANCHOR_BYTES + 1)
        except Exception as exc:
            raise AnchorStoreError(f"anchor get failed: {type(exc).__name__}") from exc
        if not isinstance(data, bytes) or len(data) > MAX_ANCHOR_BYTES:
            raise AnchorStoreError("anchor object too large")
        return data

    async def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            try:
                page = await asyncio.to_thread(self._client.list_objects_v2, **params)
            except Exception as exc:
                raise AnchorStoreError(f"anchor list failed: {type(exc).__name__}") from exc
            keys.extend(str(o["Key"]) for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return sorted(keys)
            token = page.get("NextContinuationToken")

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Ingestion, storage, scanning and parsing settings (environment, prefix ``PAIRLY_``).

Kept apart from ``pairly_core.config.Settings`` so services that never touch documents
do not need storage credentials. Fail closed: the bucket and the S3 keys have no
defaults, and ``load_ingestion_settings`` raises ``ConfigError`` listing every problem.

Intake policies are per workflow: ``PAIRLY_INTAKE_POLICIES`` is a JSON object such as
``{"pay": {"allowed_types": ["application/pdf"], "max_bytes": 10485760}}``. A workflow
without an entry gets the default policy (``PAIRLY_INTAKE_DEFAULT_TYPES`` and
``PAIRLY_INTAKE_DEFAULT_MAX_BYTES``). No policy can exceed ``PAIRLY_MAX_UPLOAD_BYTES``.

Native development backends (``make dev``, no containers), refused outside development
and test:

* ``PAIRLY_OBJECT_STORE=filesystem``: objects are files under
  ``PAIRLY_OBJECT_STORE_DIR`` (``pairly_core.ingestion.fs_storage``) and signed GET URLs
  point at the API's ``/v1/_dev/objects`` route (``PAIRLY_PUBLIC_API_URL``).
* ``PAIRLY_VIRUS_SCANNER=dev-fake``: the fake scanner, which only detects the EICAR test
  string. It is not a virus scan; production needs clamd (``PAIRLY_CLAMAV_HOST``).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from pairly_core.config import ENV_PREFIX, ConfigError, Environment
from pairly_core.ingestion.sniff import DEFAULT_ALLOWED_TYPES, KNOWN_TYPES

MIB = 1024 * 1024
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_WORKFLOW_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class IntakePolicy(BaseModel):
    """What one workflow accepts: sniffed media types and a maximum size in bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_types: frozenset[str]
    max_bytes: int = Field(ge=1)

    @field_validator("allowed_types")
    @classmethod
    def _known_types(cls, value: frozenset[str]) -> frozenset[str]:
        unknown = sorted(value - KNOWN_TYPES)
        if unknown:
            raise ValueError(f"unknown media types {unknown}; known: {sorted(KNOWN_TYPES)}")
        if not value:
            raise ValueError("allowed_types must not be empty")
        return value


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra="ignore", frozen=True
    )

    env: Environment = Environment.PRODUCTION

    # ---------------------------------------------------------------- object storage
    # Internal endpoint (containers talk to MinIO here). None means AWS S3 itself.
    s3_endpoint_url: str | None = None
    # Endpoint the browser can reach; signed URLs are made for this host.
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    # Signed GET URLs are short lived on purpose.
    signed_url_ttl_seconds: int = Field(default=60, ge=10, le=900)
    # "s3" (MinIO, AWS S3), or "filesystem" for native development and tests only.
    object_store: Literal["s3", "filesystem"] = "s3"
    # Root folder of the filesystem store (one subfolder per bucket).
    object_store_dir: str | None = None
    # Base of the filesystem store's signed URLs (the API as the browser sees it).
    public_api_url: str = "http://localhost:8000"

    # ---------------------------------------------------------------- queue
    # Optional for the API: without it, uploads are picked up by the worker's sweeper.
    redis_url: SecretStr | None = None

    # ---------------------------------------------------------------- intake limits
    max_upload_bytes: int = Field(default=25 * MIB, ge=1024, le=512 * MIB)
    intake_default_types: Annotated[frozenset[str], NoDecode] = DEFAULT_ALLOWED_TYPES
    intake_default_max_bytes: int = Field(default=20 * MIB, ge=1)
    intake_policies: dict[str, IntakePolicy] = Field(default_factory=dict)
    # Wall clock for receiving one upload body (slow clients are cut off).
    upload_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    # ---------------------------------------------------------------- inbound email
    # Addresses look like <workflow>+<org-slug>@<inbound_email_domain>.
    inbound_email_domain: str = "inbound.pairly.localhost"
    max_email_bytes: int = Field(default=40 * MIB, ge=1024, le=512 * MIB)
    max_email_attachments: int = Field(default=20, ge=1, le=200)

    # ---------------------------------------------------------------- virus scan
    # None: no scanner configured. Scans then error and documents wait for a human.
    clamav_host: str | None = None
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # "clamd" (ClamAV at clamav_host), or "dev-fake" for native development and tests
    # only: detects the EICAR test string and nothing else.
    virus_scanner: Literal["clamd", "dev-fake"] = "clamd"
    scan_max_attempts: int = Field(default=5, ge=1, le=50)

    # ---------------------------------------------------------------- parsing sandbox
    parse_wall_seconds: float = Field(default=120.0, gt=0, le=3600)
    parse_cpu_seconds: int = Field(default=90, ge=1, le=3600)
    parse_memory_mb: int = Field(default=1536, ge=64, le=16384)
    parse_max_output_mb: int = Field(default=64, ge=1, le=1024)
    parse_max_pages: int = Field(default=200, ge=1, le=5000)
    # Sandboxed parses running at once in one worker (each may use parse_memory_mb).
    parse_concurrency: int = Field(default=1, ge=1, le=16)
    ocr_languages: str = "eng+ara"
    ocr_min_confidence: float = Field(default=60.0, ge=0, le=100)
    ocr_dpi: int = Field(default=300, ge=72, le=600)

    @field_validator("intake_default_types", mode="before")
    @classmethod
    def _csv_types(cls, value: Any) -> Any:
        if isinstance(value, str):
            return frozenset(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("intake_default_types")
    @classmethod
    def _default_types_known(cls, value: frozenset[str]) -> frozenset[str]:
        IntakePolicy(allowed_types=value, max_bytes=1)
        return value

    @field_validator("intake_policies")
    @classmethod
    def _policy_keys(cls, value: dict[str, IntakePolicy]) -> dict[str, IntakePolicy]:
        for key in value:
            if not _WORKFLOW_RE.fullmatch(key):
                raise ValueError(f"invalid workflow id {key!r} in intake policies")
        return value

    @field_validator("inbound_email_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        value = value.strip().lower()
        if not _DOMAIN_RE.fullmatch(value):
            raise ValueError("must be a domain name such as inbound.example.com")
        return value

    @field_validator("s3_endpoint_url", "s3_public_endpoint_url")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.path
            not in {
                "",
                "/",
            }
        ):
            raise ValueError("must be an http(s) URL with a host and no path")
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _limits(self) -> IngestionSettings:
        too_big = [
            wf for wf, p in self.intake_policies.items() if p.max_bytes > self.max_upload_bytes
        ]
        if self.intake_default_max_bytes > self.max_upload_bytes:
            too_big.append("default")
        if too_big:
            raise ValueError(f"intake max_bytes above max_upload_bytes for {sorted(too_big)}")
        insecure_ok = self.env in {Environment.DEVELOPMENT, Environment.TEST}
        for url in (self.s3_endpoint_url, self.s3_public_endpoint_url):
            if url and url.startswith("http://") and not insecure_ok:
                raise ValueError("S3 endpoints must use https outside development and test")
        if self.object_store == "filesystem":
            if not insecure_ok:
                raise ValueError("the filesystem object store is for development and test only")
            if not (self.object_store_dir or "").strip():
                raise ValueError("the filesystem object store needs PAIRLY_OBJECT_STORE_DIR")
            parts = urlsplit(self.public_api_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("public_api_url must be an absolute http(s) URL")
        if self.virus_scanner == "dev-fake" and not insecure_ok:
            raise ValueError("the dev-fake virus scanner is for development and test only")
        return self

    def policy_for(self, workflow: str) -> IntakePolicy:
        policy = self.intake_policies.get(workflow)
        if policy is not None:
            return policy
        return IntakePolicy(
            allowed_types=self.intake_default_types, max_bytes=self.intake_default_max_bytes
        )


def _env_name(loc: tuple[int | str, ...]) -> str:
    return f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "settings"


def load_ingestion_settings(**overrides: Any) -> IngestionSettings:
    """Build ``IngestionSettings`` from the environment, or raise ``ConfigError``."""
    try:
        return IngestionSettings(**overrides)
    except ValidationError as exc:
        missing = sorted({_env_name(e["loc"]) for e in exc.errors() if e["type"] == "missing"})
        # Never echo input values: they can hold credentials.
        invalid = sorted(
            f"{_env_name(e['loc'])}: {e['msg']}" for e in exc.errors() if e["type"] != "missing"
        )
        lines = []
        if missing:
            lines.append("missing required settings: " + ", ".join(missing))
        if invalid:
            lines.append("invalid settings: " + "; ".join(invalid))
        raise ConfigError(". ".join(lines)) from None

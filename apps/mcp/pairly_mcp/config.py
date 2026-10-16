# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""MCP server settings, from the environment.

* ``PAIRLY_API_URL``  the API base URL (default ``http://127.0.0.1:8000``, the native
  stack). Plain ``http`` is only accepted for a loopback host.
* ``PAIRLY_API_KEY``  an API key (``pk_live_...`` or ``pk_test_...``) with ``read`` and
  ``propose`` scopes. Add ``core.approval.read`` so ``list_my_approvals`` shows the
  inbox of the person who created the key.
* ``PAIRLY_TOKEN``    or a person's JWT (short-lived, from the web app), with
* ``PAIRLY_ORG``      the organisation id (required with a JWT, optional with a key).
* ``PAIRLY_WEB_URL``  the web app, for "open in Pairly" links (optional).

Exactly one of ``PAIRLY_API_KEY`` and ``PAIRLY_TOKEN``. The credential is never logged
and never returned by a tool.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

DEFAULT_API_URL: Final = "http://127.0.0.1:8000"
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """The environment does not configure the server (the message never has a secret)."""


@dataclass(frozen=True, slots=True)
class McpConfig:
    api_url: str
    credential: str = field(repr=False)
    org_id: uuid.UUID | None = None
    web_url: str | None = None

    @property
    def uses_api_key(self) -> bool:
        return self.credential.startswith("pk_")


def _check_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{name} must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ConfigError(f"{name} must use https unless it points at this machine")
    if parts.username or parts.password:
        raise ConfigError(f"{name} must not contain credentials")
    return value.rstrip("/")


def load_config(environ: Mapping[str, str] | None = None) -> McpConfig:
    env = os.environ if environ is None else environ
    key = env.get("PAIRLY_API_KEY", "").strip()
    token = env.get("PAIRLY_TOKEN", "").strip()
    if bool(key) == bool(token):
        raise ConfigError("set exactly one of PAIRLY_API_KEY and PAIRLY_TOKEN")
    if key and not key.startswith(("pk_live_", "pk_test_")):
        raise ConfigError("PAIRLY_API_KEY must be a Pairly API key (pk_live_... or pk_test_...)")
    org_raw = env.get("PAIRLY_ORG", "").strip()
    org: uuid.UUID | None = None
    if org_raw:
        try:
            org = uuid.UUID(org_raw)
        except ValueError:
            raise ConfigError("PAIRLY_ORG must be an organisation id") from None
    if token and org is None:
        raise ConfigError("PAIRLY_ORG is required with PAIRLY_TOKEN")
    api_url = _check_url("PAIRLY_API_URL", env.get("PAIRLY_API_URL", "").strip() or DEFAULT_API_URL)
    web_raw = env.get("PAIRLY_WEB_URL", "").strip()
    web_url = _check_url("PAIRLY_WEB_URL", web_raw) if web_raw else None
    return McpConfig(api_url=api_url, credential=key or token, org_id=org, web_url=web_url)

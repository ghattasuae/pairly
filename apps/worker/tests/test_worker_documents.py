# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Document job setup: fail closed without storage settings, scanner selection.

The jobs themselves run against Postgres in apps/api/tests/test_documents.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from pairly_core.config import ConfigError, Environment
from pairly_core.ingestion.scan import ClamdScanner, UnavailableScanner
from pairly_core.ingestion.storage import S3ObjectStore
from pairly_worker.documents import setup_documents

S3_VARS = ("PAIRLY_S3_BUCKET", "PAIRLY_S3_ACCESS_KEY", "PAIRLY_S3_SECRET_KEY", "PAIRLY_CLAMAV_HOST")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in S3_VARS:
        monkeypatch.delenv(name, raising=False)


def _storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIRLY_S3_BUCKET", "documents")
    monkeypatch.setenv("PAIRLY_S3_ACCESS_KEY", "worker")
    monkeypatch.setenv("PAIRLY_S3_SECRET_KEY", "synthetic-secret")


def test_missing_storage_fails_closed_outside_tests() -> None:
    with pytest.raises(ConfigError):
        setup_documents({}, Environment.PRODUCTION)
    ctx: dict[str, Any] = {}
    setup_documents(ctx, Environment.TEST)
    assert ctx == {}


def test_scanner_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    _storage_env(monkeypatch)
    ctx: dict[str, Any] = {}
    setup_documents(ctx, Environment.DEVELOPMENT)
    assert isinstance(ctx["store"], S3ObjectStore)
    assert isinstance(ctx["scanner"], UnavailableScanner)
    assert ctx["parse_config"].limits.memory_bytes > 0
    monkeypatch.setenv("PAIRLY_CLAMAV_HOST", "clamav")
    setup_documents(ctx, Environment.DEVELOPMENT)
    assert isinstance(ctx["scanner"], ClamdScanner)
    assert ctx["scanner"].host == "clamav"

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Liveness and readiness probes (unauthenticated, unversioned)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from pairly_core.db import check_app_role, ping
from pairly_core.errors import ServiceUnavailableError
from pairly_core.identity import Context

router = APIRouter(tags=["health"])
log = structlog.get_logger(__name__)


class Status(BaseModel):
    status: str


@router.get("/healthz", response_model=Status)
async def healthz() -> Status:
    """The process is up. Does not touch dependencies."""
    return Status(status="ok")


@router.get("/readyz", response_model=Status)
async def readyz(ctx: Context) -> Status:
    """The service can take traffic: the database answers, as a role that obeys RLS."""
    try:
        await ping(ctx.engine)
        await check_app_role(ctx.engine)
    except Exception as exc:
        log.warning("readiness_failed", error=type(exc).__name__)
        raise ServiceUnavailableError("database not ready") from None
    return Status(status="ok")

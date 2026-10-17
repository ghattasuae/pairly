# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Small helpers shared by the Hire routes."""

from __future__ import annotations

import os
from typing import Final

from fastapi import Request

from pairly_core.errors import ServiceUnavailableError
from pairly_core.llm.router import ModelRouter
from pairly_core.workflow.runtime import WorkflowRuntime

DEFAULT_WEB_URL: Final = "http://localhost:3100"


def web_url(request: Request) -> str:
    """The public web app URL (links in feeds, JSON-LD and emails)."""
    settings = getattr(request.app.state, "notifications", None)
    url = getattr(settings, "public_web_url", None)
    if isinstance(url, str) and url:
        return url.rstrip("/")
    return os.environ.get("PAIRLY_PUBLIC_WEB_URL", DEFAULT_WEB_URL).rstrip("/")


def model_router(request: Request) -> ModelRouter:
    """The process's model router (``GraphServices.router``). Missing: 503, fail closed."""
    runtime = getattr(request.app.state, "workflow_runtime", None)
    router = runtime.services.router if isinstance(runtime, WorkflowRuntime) else None
    if router is None:
        raise ServiceUnavailableError("AI drafting is not available here. Write the text yourself.")
    return router

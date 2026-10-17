# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Model router: providers, routing, recording and replay, budgets and tracing.

Entry point: ``ModelRouter.complete`` (build one with ``build_router``). Every error a
caller can get is a ``ModelUnavailable``: route the item to a human queue.
"""

from __future__ import annotations

from pairly_core.llm.errors import BudgetExceeded, InputTooLarge, ModelUnavailable, RecordingMissing
from pairly_core.llm.factory import build_providers, build_router
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode, load_llm_settings
from pairly_core.llm.types import CallContext, ReplayResult, RouterResult, TaskSpec

__all__ = [
    "BudgetExceeded",
    "CallContext",
    "InputTooLarge",
    "LLMSettings",
    "ModelMode",
    "ModelRouter",
    "ModelUnavailable",
    "RecordingMissing",
    "ReplayResult",
    "RouterResult",
    "TaskSpec",
    "build_providers",
    "build_router",
    "load_llm_settings",
]

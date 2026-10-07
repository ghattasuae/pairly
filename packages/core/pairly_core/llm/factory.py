# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Build a ``ModelRouter`` from settings (used by the API and the worker)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.llm.budget import BudgetManager, usd_to_micros
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.prompt_registry import PromptRegistry, Root, core_prompt_root
from pairly_core.llm.providers.anthropic import AnthropicProvider
from pairly_core.llm.providers.base import ModelProvider
from pairly_core.llm.providers.openai import OpenAIProvider
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings
from pairly_core.llm.tracing import Redactor, RegexRedactor, build_tracer


def build_providers(settings: LLMSettings) -> dict[str, ModelProvider]:
    """Real providers. One without a key reports ``available = False``."""
    anthropic_key = settings.anthropic_api_key
    openai_key = settings.openai_api_key
    timeout = settings.llm_request_timeout_seconds
    return {
        "anthropic": AnthropicProvider(
            anthropic_key.get_secret_value() if anthropic_key else None, timeout_seconds=timeout
        ),
        "openai": OpenAIProvider(
            openai_key.get_secret_value() if openai_key else None, timeout_seconds=timeout
        ),
    }


def build_router(
    settings: LLMSettings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    prompt_roots: Sequence[Root] = (),
    redactor: Redactor | None = None,
    providers: dict[str, ModelProvider] | None = None,
) -> ModelRouter:
    prices = load_prices(settings.llm_prices_file)
    catalog = load_catalog(prices, settings.llm_models_file)
    redactor = redactor or RegexRedactor()
    return ModelRouter(
        settings=settings,
        providers=providers if providers is not None else build_providers(settings),
        catalog=catalog,
        prices=prices,
        prompts=PromptRegistry([*prompt_roots, core_prompt_root()]),
        sessionmaker=sessionmaker,
        budget=BudgetManager(
            default_limit_micros=usd_to_micros(settings.llm_monthly_budget_usd),
            reservation_ttl_seconds=settings.llm_reservation_ttl_seconds,
        ),
        tracer=build_tracer(settings, redactor),
        redactor=redactor,
    )

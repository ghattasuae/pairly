# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Model catalog (tiers to models per provider) and the price table.

Both are versioned YAML files shipped in ``pairly_core/llm/data`` and replaceable with
``PAIRLY_LLM_MODELS_FILE`` and ``PAIRLY_LLM_PRICES_FILE``. Prices are never hardcoded
in logic: every cost comes from ``PriceTable``, and the price table names its source
URL and retrieval date.

Loading fails closed: a catalog model without a price, an unknown tier or a malformed
file raises ``CatalogError`` at startup.

Money is integer micro-USD. A price of ``$X per million tokens`` is exactly ``X``
micro-USD per token, so ``cost_micros = ceil(tokens * X)`` per token class.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

TIER_ORDER = ("small", "large")


class CatalogError(RuntimeError):
    """The model catalog or price table is missing, malformed or inconsistent."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelEntry(_Strict):
    model: str = Field(min_length=1, max_length=128)
    # Current Claude models (Sonnet 5.5, Opus 5.5) and the GPT-6 family reject a
    # non-default temperature. Temperature 0 is sent only where the model accepts it.
    supports_temperature: bool = False
    # Optional provider-specific effort / reasoning level (None: provider default).
    effort: str | None = None


class CatalogFile(_Strict):
    version: str
    tiers: list[str]
    provider_order: list[str]
    providers: dict[str, dict[str, ModelEntry]]

    @field_validator("tiers")
    @classmethod
    def _known_tiers(cls, value: list[str]) -> list[str]:
        if not value or any(t not in TIER_ORDER for t in value) or len(set(value)) != len(value):
            raise ValueError(f"tiers must be distinct values from {TIER_ORDER}")
        return sorted(value, key=TIER_ORDER.index)


class PriceEntry(_Strict):
    provider: str
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)
    cache_write: Decimal = Field(ge=0)


class PriceSource(_Strict):
    url: str
    retrieved: str


class PriceFile(_Strict):
    version: str
    currency: str
    unit: str
    sources: dict[str, PriceSource]
    models: dict[str, PriceEntry]

    @field_validator("currency")
    @classmethod
    def _usd(cls, value: str) -> str:
        if value != "USD":
            raise ValueError("only USD prices are supported")
        return value

    @field_validator("unit")
    @classmethod
    def _per_million(cls, value: str) -> str:
        if value != "per_million_tokens":
            raise ValueError("unit must be per_million_tokens")
        return value


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalised token counts. ``input_tokens`` excludes cache reads and writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _micros(tokens: int, price: Decimal) -> int:
    return math.ceil(Decimal(tokens) * price)


class PriceTable:
    def __init__(self, data: PriceFile) -> None:
        self.version = data.version
        self.sources = data.sources
        self._models = data.models

    def has(self, model: str) -> bool:
        return model in self._models

    def entry(self, model: str) -> PriceEntry:
        try:
            return self._models[model]
        except KeyError:
            raise CatalogError(f"no price for model {model!r}") from None

    def cost_micros(self, model: str, usage: Usage) -> int:
        price = self.entry(model)
        return (
            _micros(usage.input_tokens, price.input)
            + _micros(usage.output_tokens, price.output)
            + _micros(usage.cache_read_tokens, price.cache_read)
            + _micros(usage.cache_write_tokens, price.cache_write)
        )

    def max_cost_micros(self, model: str, *, input_tokens: int, output_tokens: int) -> int:
        """Upper bound for budget reservations: every input token at the highest input
        rate (a cache write costs more than plain input) and every output token."""
        price = self.entry(model)
        top_input = max(price.input, price.cache_write, price.cache_read)
        return _micros(input_tokens, top_input) + _micros(output_tokens, price.output)


class ModelCatalog:
    def __init__(self, data: CatalogFile, prices: PriceTable) -> None:
        self.version = data.version
        self.tiers: tuple[str, ...] = tuple(data.tiers)
        self.provider_order: tuple[str, ...] = tuple(data.provider_order)
        self._providers = data.providers
        unknown = [p for p in self.provider_order if p not in self._providers]
        if unknown:
            raise CatalogError(f"provider_order names unknown providers: {unknown}")
        for provider, tiers in self._providers.items():
            for tier, entry in tiers.items():
                if tier not in self.tiers:
                    raise CatalogError(f"{provider}: unknown tier {tier!r}")
                if not prices.has(entry.model):
                    raise CatalogError(f"{provider}/{tier}: model {entry.model!r} has no price")
                if prices.entry(entry.model).provider != provider:
                    raise CatalogError(f"{entry.model!r} is priced under another provider")

    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def model_for(self, provider: str, tier: str) -> ModelEntry | None:
        return self._providers.get(provider, {}).get(tier)

    def tiers_from(self, start: str, up_to: str | None = None) -> tuple[str, ...]:
        if start not in self.tiers:
            raise CatalogError(f"unknown tier {start!r}")
        if up_to is not None and up_to not in self.tiers:
            raise CatalogError(f"unknown tier {up_to!r}")
        end = self.tiers.index(up_to) if up_to is not None else len(self.tiers) - 1
        return self.tiers[self.tiers.index(start) : end + 1]


def _read_yaml(path: Path | None, default_name: str) -> Any:
    try:
        if path is None:
            text = (
                resources.files("pairly_core.llm")
                .joinpath("data", default_name)
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
        return yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read {path or default_name}: {exc}") from None


def load_prices(path: Path | None = None) -> PriceTable:
    try:
        return PriceTable(PriceFile.model_validate(_read_yaml(path, "prices.yaml")))
    except ValidationError as exc:
        raise CatalogError(f"invalid price table: {exc}") from None


def load_catalog(prices: PriceTable, path: Path | None = None) -> ModelCatalog:
    try:
        data = CatalogFile.model_validate(_read_yaml(path, "models.yaml"))
    except ValidationError as exc:
        raise CatalogError(f"invalid model catalog: {exc}") from None
    return ModelCatalog(data, prices)

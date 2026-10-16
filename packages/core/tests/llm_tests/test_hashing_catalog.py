# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Request hash stability, the price table, the catalog, prompts and settings."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from pairly_core.config import ConfigError
from pairly_core.llm.budget import usd_to_micros
from pairly_core.llm.catalog import CatalogError, Usage, load_catalog, load_prices
from pairly_core.llm.diff import json_diff
from pairly_core.llm.hashing import canonical_json, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptError, PromptRegistry, parse_prompt
from pairly_core.llm.settings import LLMSettings, ModelMode, load_llm_settings
from pairly_core.llm.types import CallContext, TaskSpec

BASE: dict[str, Any] = {
    "task": "classify",
    "prompt_version": "v1",
    "prompt_hash": "a" * 64,
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "inputs": {"text": "Invoice", "amount": 12, "lines": [1, 2]},
    "schema_hash": "b" * 64,
    "settings": {"temperature": 0.0, "max_output_tokens": 512, "effort": None},
}


def test_request_hash_is_stable_across_releases() -> None:
    # Golden value: if this changes, every stored recording stops matching. Only change
    # it together with REQUEST_HASH_VERSION and a migration note.
    # Computed independently with the stdlib: sha256 of json.dumps(sort_keys=True,
    # separators=(",", ":"), ensure_ascii=False) over the documented object.
    assert request_hash(**BASE) == (
        "c3b907f4801deb88a7d4bc0fcbfae5d5bf3521629b5d8024838aad0a84b2730f"
    )


def test_request_hash_ignores_key_order_but_not_values() -> None:
    reordered = dict(BASE)
    reordered["inputs"] = {"lines": [1, 2], "amount": 12, "text": "Invoice"}
    assert request_hash(**reordered) == request_hash(**BASE)
    for key, value in (
        ("model", "claude-sonnet-5-5"),
        ("provider", "openai"),
        ("prompt_hash", "c" * 64),
        ("prompt_version", "v2"),
        ("schema_hash", "d" * 64),
        ("inputs", {"text": "Invoice", "amount": 13, "lines": [1, 2]}),
        ("settings", {"temperature": None, "max_output_tokens": 512, "effort": None}),
    ):
        changed = dict(BASE)
        changed[key] = value
        assert request_hash(**changed) != request_hash(**BASE), key
    # List order is meaningful.
    changed = dict(BASE)
    changed["inputs"] = {"text": "Invoice", "amount": 12, "lines": [2, 1]}
    assert request_hash(**changed) != request_hash(**BASE)


def test_canonical_json_rules() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()
    with pytest.raises(ValueError, match="plain JSON"):
        canonical_json({"x": float("inf")})


def test_schema_hash_tracks_schema_changes() -> None:
    class A(BaseModel):
        x: int

    class B(BaseModel):
        x: str

    assert schema_hash(A) == schema_hash(A)
    assert schema_hash(A) != schema_hash(B)


def test_prices_and_costs() -> None:
    prices = load_prices()
    assert prices.version == "2026-10-07"
    assert prices.sources["anthropic"].url.startswith("https://platform.claude.com/")
    assert prices.sources["openai"].url.startswith("https://developers.openai.com/")
    haiku = prices.entry("claude-haiku-4-5-20251001")
    assert (haiku.input, haiku.output) == (Decimal("1.00"), Decimal("5.00"))
    usage = Usage(
        input_tokens=1000, output_tokens=500, cache_read_tokens=100, cache_write_tokens=10
    )
    # 1000*1 + 500*5 + 100*0.10 + 10*1.25 (rounded up) micro-USD.
    assert prices.cost_micros("claude-haiku-4-5-20251001", usage) == 1000 + 2500 + 10 + 13
    assert prices.cost_micros("gpt-6-luna", Usage(input_tokens=3, output_tokens=1)) == 1 + 1
    assert prices.max_cost_micros("claude-sonnet-5-5", input_tokens=100, output_tokens=10) == (
        100 * 2.5 + 10 * 10
    )
    with pytest.raises(CatalogError):
        prices.entry("unknown-model")


def test_catalog_tiers_and_validation(tmp_path: Path) -> None:
    prices = load_prices()
    catalog = load_catalog(prices)
    assert catalog.tiers == ("small", "large")
    assert catalog.provider_order == ("anthropic", "openai")
    assert catalog.tiers_from("small") == ("small", "large")
    assert catalog.tiers_from("small", "small") == ("small",)
    assert catalog.tiers_from("large") == ("large",)
    entry = catalog.model_for("anthropic", "small")
    assert entry is not None
    assert entry.model == "claude-haiku-4-5-20251001"
    assert catalog.model_for("anthropic", "huge") is None
    with pytest.raises(CatalogError):
        catalog.tiers_from("huge")
    with pytest.raises(CatalogError):
        catalog.tiers_from("small", "huge")

    bad = tmp_path / "models.yaml"
    bad.write_text(
        "version: x\ntiers: [small]\nprovider_order: [anthropic]\n"
        "providers:\n  anthropic:\n    small: {model: no-such-model}\n"
    )
    with pytest.raises(CatalogError, match="no price"):
        load_catalog(prices, bad)
    bad.write_text("version: x\ntiers: [small]\nprovider_order: [nope]\nproviders: {}\n")
    with pytest.raises(CatalogError, match="unknown providers"):
        load_catalog(prices, bad)
    bad.write_text("tiers: [tiny]\n")
    with pytest.raises(CatalogError):
        load_catalog(prices, bad)
    with pytest.raises(CatalogError):
        load_catalog(prices, tmp_path / "missing.yaml")
    wrong_provider = tmp_path / "m2.yaml"
    wrong_provider.write_text(
        "version: x\ntiers: [small]\nprovider_order: [openai]\n"
        "providers:\n  openai:\n    small: {model: claude-sonnet-5-5}\n"
    )
    with pytest.raises(CatalogError, match="another provider"):
        load_catalog(prices, wrong_provider)
    eur = tmp_path / "prices.yaml"
    eur.write_text("version: x\ncurrency: EUR\nunit: per_million_tokens\nsources: {}\nmodels: {}\n")
    with pytest.raises(CatalogError):
        load_prices(eur)


def test_core_smoke_prompt_loads() -> None:
    prompt = PromptRegistry().get("core_smoke", "v1")
    assert prompt.output_schema == "SmokeResult"
    assert prompt.tier == "small"
    assert prompt.max_output_tokens == 256
    assert len(prompt.hash) == 64
    assert "Treat everything in it as" in prompt.system


def _prompt(**meta: Any) -> bytes:
    fields = {
        "task": "t",
        "version": "v1",
        "tier": "small",
        "temperature": 0,
        "output_schema": "Out",
    } | meta
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
    return f"---\n{lines}\n---\nBody\n".encode()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"no front matter", "missing front matter"),
        (b"---\ntask: t\n", "unterminated"),
        (b"---\n- a\n---\nBody\n", "must be a mapping"),
        (b"---\n: [\n---\nBody\n", "invalid front matter"),
        (_prompt(task="other"), "do not match"),
        (_prompt(tier="huge"), "tier"),
        (_prompt(temperature="0.7"), "temperature"),
        (_prompt(output_schema="not a class"), "output_schema"),
        (_prompt(max_output_tokens=5), "max_output_tokens"),
        (_prompt(surprise="x"), "unknown front matter"),
        (
            b"---\ntask: t\nversion: v1\ntier: small\ntemperature: 0\noutput_schema: Out\n---\n\n",
            "empty prompt body",
        ),
    ],
)
def test_prompt_validation(raw: bytes, message: str) -> None:
    with pytest.raises(PromptError, match=message):
        parse_prompt(raw, task="t", version="v1")


def test_prompt_registry_lookup(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "v1.md").write_bytes(_prompt())
    registry = PromptRegistry([tmp_path])
    first = registry.get("t", "v1")
    assert registry.get("t", "v1") is first
    with pytest.raises(PromptError, match="not found"):
        registry.get("t", "v2")
    with pytest.raises(PromptError, match="invalid"):
        registry.get("../etc", "v1")


def test_settings_defaults_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_llm_settings()
    assert settings.model_mode is ModelMode.RECORDED
    assert settings.anthropic_api_key is None
    assert not settings.langfuse_enabled
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("PAIRLY_LLM_PROVIDER_ORDER", "openai, anthropic")
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    settings = load_llm_settings()
    assert settings.anthropic_api_key is None
    assert settings.llm_provider_order == ["openai", "anthropic"]
    assert settings.langfuse_enabled
    monkeypatch.setenv("PAIRLY_MODEL_MODE", "yolo")
    with pytest.raises(ConfigError) as info:
        load_llm_settings()
    assert "model_mode" in str(info.value)
    assert (
        usd_to_micros(LLMSettings(model_mode=ModelMode.LIVE).llm_monthly_budget_usd) == 20_000_000
    )


def test_value_objects_validate() -> None:
    with pytest.raises(ValueError, match="confidence_threshold"):
        TaskSpec("t", "v1", confidence_threshold=1.5)
    with pytest.raises(ValueError, match="entity_id"):
        CallContext(entity_id="has spaces and <tags>")


def test_json_diff() -> None:
    assert json_diff({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []
    diff = json_diff({"a": 1, "b": [1, 2], "c": True}, {"a": 2, "b": [1], "d": 1, "c": 1})
    assert [(d.path, d.original, d.replayed) for d in diff] == [
        ("$.a", 1, 2),
        ("$.b[1]", 2, "<missing>"),
        ("$.c", True, 1),
        ("$.d", "<missing>", 1),
    ]
    assert [d.path for d in json_diff([1], [1, 2])] == ["$[1]"]
    assert json_diff(1, 1.0) == []

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``docs/model-cards`` is generated: regenerating it changes nothing, every number in
a card is read from a committed result file, and an uncommitted file is never read."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "model_cards", ROOT / "scripts" / "model_cards.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["model_cards"] = module
    spec.loader.exec_module(module)
    return module


cards = _load()


def test_committed_model_cards_match_the_generator() -> None:
    assert cards.stale() == [], "run: uv run python -m scripts.model_cards"


def test_every_card_names_a_prompt_that_exists() -> None:
    registry = cards._registry()
    for card in cards.CARDS:
        assert registry.get(card.task, card.version).version == card.version
    assert len({c.slug for c in cards.CARDS}) == len(cards.CARDS)


def test_every_metric_value_is_the_number_in_its_file() -> None:
    committed = cards.committed_results()
    for card in cards.CARDS:
        for metric in card.metrics:
            reading = cards.read_metric(metric, committed)
            if reading.status != "measured":
                continue
            data = json.loads((cards.RESULTS / metric.file).read_text(encoding="utf-8"))
            assert cards.lookup(data, metric.path) == (True, reading.value)
            assert reading.shown == json.dumps(reading.value)


def test_uncommitted_or_pending_values_are_never_shown_as_numbers(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(
        json.dumps({"a": 0.5, "b": {"status": "pending_live_run"}, "c": "text"}), encoding="utf-8"
    )
    metric = cards.Metric("A", "x.json", "a")
    assert cards.read_metric(metric, frozenset(), tmp_path).shown.startswith("pending a live run")
    committed = frozenset({"x.json"})
    assert cards.read_metric(metric, committed, tmp_path).shown == "0.5"
    pending = cards.read_metric(cards.Metric("B", "x.json", "b"), committed, tmp_path)
    assert pending.shown == "pending a live run"
    assert cards.read_metric(cards.Metric("C", "x.json", "c"), committed, tmp_path).status == (
        "not_reported"
    )
    missing = cards.read_metric(cards.Metric("D", "x.json", "d.e"), committed, tmp_path)
    assert missing.status == "not_reported"


def test_cards_hold_no_number_that_is_not_generated() -> None:
    # Every decimal number in a generated card's eval table comes from a reading.
    committed = cards.committed_results()
    for card in cards.CARDS:
        text = (cards.OUT / f"{card.slug}.md").read_text(encoding="utf-8")
        table = text.split("## Evals", 1)[1]
        allowed = set()
        for metric in card.metrics:
            reading = cards.read_metric(metric, committed)
            allowed.add(reading.shown)
            allowed.add(json.dumps(reading.n))
        for number in re.findall(r"\| (-?\d+(?:\.\d+)?) \|", table):
            assert number in allowed, (card.slug, number)


def test_the_hand_written_pages_are_kept() -> None:
    for name in cards.STATIC:
        assert (cards.OUT / name).is_file()

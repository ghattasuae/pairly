# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Unit tests for workflow plugin discovery."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import EntryPoint

import pytest

from pairly_core import plugins
from pairly_core.plugins import ENTRY_POINT_GROUP, PluginError, discover_plugins

_FAKE_MODULE = "_pairly_fake_plugins"


@dataclass(frozen=True)
class _Plugin:
    id: str
    name: str


class _NotAPlugin:
    id = "broken"


@pytest.fixture
def fake_module() -> Iterator[types.ModuleType]:
    module = types.ModuleType(_FAKE_MODULE)
    module.alpha = _Plugin(id="alpha", name="Alpha")  # type: ignore[attr-defined]
    module.beta = _Plugin(id="beta", name="Beta")  # type: ignore[attr-defined]
    module.alpha_again = _Plugin(id="alpha", name="Alpha 2")  # type: ignore[attr-defined]
    module.mismatch = _Plugin(id="other", name="Other")  # type: ignore[attr-defined]
    module.bad_id = _Plugin(id="Bad-Id", name="Bad")  # type: ignore[attr-defined]
    module.not_plugin = _NotAPlugin()  # type: ignore[attr-defined]
    sys.modules[_FAKE_MODULE] = module
    yield module
    del sys.modules[_FAKE_MODULE]


def _use_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[EntryPoint]) -> None:
    def fake_entry_points(*, group: str) -> list[EntryPoint]:
        return [ep for ep in eps if ep.group == group]

    monkeypatch.setattr(plugins, "entry_points", fake_entry_points)


def _ep(name: str, attr: str, group: str = ENTRY_POINT_GROUP) -> EntryPoint:
    return EntryPoint(name=name, value=f"{_FAKE_MODULE}:{attr}", group=group)


@pytest.mark.usefixtures("fake_module")
def test_discovers_plugins_keyed_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_entry_points(
        monkeypatch,
        [_ep("beta", "beta"), _ep("alpha", "alpha"), _ep("ignored", "alpha", group="other.group")],
    )

    found = discover_plugins()

    assert list(found) == ["alpha", "beta"]
    assert found["alpha"].name == "Alpha"
    assert isinstance(found["beta"], plugins.WorkflowPlugin)


def test_no_plugins_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_entry_points(monkeypatch, [])
    assert discover_plugins() == {}


@pytest.mark.usefixtures("fake_module")
@pytest.mark.parametrize(
    ("eps", "message"),
    [
        ([_ep("missing", "does_not_exist")], "failed to load"),
        ([_ep("broken", "not_plugin")], "does not satisfy"),
        ([_ep("Bad-Id", "bad_id")], "invalid id"),
        ([_ep("alpha", "mismatch")], "does not match"),
        ([_ep("alpha", "alpha"), _ep("alpha", "alpha_again")], "duplicate"),
    ],
)
def test_invalid_plugins_fail_closed(
    monkeypatch: pytest.MonkeyPatch, eps: list[EntryPoint], message: str
) -> None:
    _use_entry_points(monkeypatch, eps)
    with pytest.raises(PluginError, match=message):
        discover_plugins()

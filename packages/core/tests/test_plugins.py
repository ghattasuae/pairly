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
from fastapi import APIRouter

from pairly_core import plugins
from pairly_core.plugins import ENTRY_POINT_GROUP, PluginError, discover_plugins
from pairly_core.rbac import PermissionSpec

_FAKE_MODULE = "_pairly_fake_plugins"


@dataclass(frozen=True)
class _Plugin:
    id: str
    name: str
    permissions: tuple[object, ...] = ()
    graphs: tuple[object, ...] = ()


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
    module.foreign_perm = _Plugin(  # type: ignore[attr-defined]
        id="alpha", name="Alpha", permissions=(PermissionSpec("beta.thing.read", "x"),)
    )
    module.bad_perm = _Plugin(id="alpha", name="Alpha", permissions=("alpha.x.read",))  # type: ignore[attr-defined]
    module.str_perms = _Plugin(id="alpha", name="Alpha", permissions="alpha.x.read")  # type: ignore[attr-defined, arg-type]
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
        ([_ep("alpha", "foreign_perm")], "outside its namespace"),
        ([_ep("alpha", "bad_perm")], "must be PermissionSpec"),
        ([_ep("alpha", "str_perms")], "must be a sequence"),
    ],
)
def test_invalid_plugins_fail_closed(
    monkeypatch: pytest.MonkeyPatch, eps: list[EntryPoint], message: str
) -> None:
    _use_entry_points(monkeypatch, eps)
    with pytest.raises(PluginError, match=message):
        discover_plugins()


# ------------------------------------------------------------------ optional hooks


@dataclass(frozen=True)
class _Hooked:
    id: str
    api_router: object = None
    demo_seeds: object = None


async def _seed(*_: object) -> plugins.SeedResult:
    return plugins.SeedResult()


def test_plugin_routers_and_demo_seeds() -> None:
    router = APIRouter()
    found = plugins.plugin_routers([_Hooked("alpha", api_router=lambda: router), _Hooked("beta")])
    assert found == [("alpha", router)]
    seeds = plugins.demo_seeds([_Hooked("alpha", demo_seeds={"alpha-demo": _seed}), _Hooked("b")])
    assert seeds == {"alpha-demo": _seed}


@pytest.mark.parametrize(
    ("plugin", "message"),
    [
        (_Hooked("alpha", api_router="not callable"), "must be callable"),
        (_Hooked("alpha", api_router=lambda: "no router"), "must return APIRouter"),
    ],
)
def test_bad_routers_fail_closed(plugin: _Hooked, message: str) -> None:
    with pytest.raises(PluginError, match=message):
        plugins.plugin_routers([plugin])


@pytest.mark.parametrize(
    ("seeds", "message"),
    [
        ({"Bad Name": _seed}, "invalid seed name"),
        ({"beta-demo": _seed}, "outside its namespace"),
        ({"alpha-demo": "not callable"}, "not callable"),
    ],
)
def test_bad_demo_seeds_fail_closed(seeds: dict[str, object], message: str) -> None:
    with pytest.raises(PluginError, match=message):
        plugins.demo_seeds([_Hooked("alpha", demo_seeds=seeds)])


def test_duplicate_demo_seeds_fail_closed() -> None:
    hooked = _Hooked("alpha", demo_seeds={"alpha-x": _seed})
    with pytest.raises(PluginError, match="duplicate"):
        plugins.demo_seeds([hooked, hooked])

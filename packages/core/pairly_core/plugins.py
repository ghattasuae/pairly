# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Workflow plugin contract and discovery.

Workflows (Pairly Hire, Pairly Pay, and any third workflow) plug into the core engine
through the ``pairly.workflows`` Python entry point group. The core engine never imports
a workflow package directly; it only discovers plugin objects at runtime.

The ``WorkflowPlugin`` protocol is intentionally minimal for now. It grows with the
engine (entity types, models, extraction schemas, detectors, rules, policy schema,
graphs, approval types, templates, router, permissions, seed hook).

Discovery fails closed: a plugin that cannot be loaded, does not satisfy the protocol,
has an invalid id, or collides with another plugin raises ``PluginError`` instead of
being silently skipped.
"""

from __future__ import annotations

import re
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, runtime_checkable

ENTRY_POINT_GROUP = "pairly.workflows"

# Plugin ids are used in permission strings (``hire.candidate.read``), table prefixes
# and URLs, so they are restricted to lowercase identifiers.
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class PluginError(Exception):
    """Raised when a workflow plugin cannot be discovered or validated."""


@runtime_checkable
class WorkflowPlugin(Protocol):
    """Contract every workflow plugin object must satisfy."""

    @property
    def id(self) -> str:
        """Stable, unique workflow id (for example ``hire`` or ``pay``)."""
        ...

    @property
    def name(self) -> str:
        """Human-readable workflow name (for example ``Pairly Hire``)."""
        ...


def _load(ep: EntryPoint) -> WorkflowPlugin:
    try:
        obj = ep.load()
    except Exception as exc:
        raise PluginError(f"failed to load workflow plugin {ep.name!r} ({ep.value})") from exc

    if not isinstance(obj, WorkflowPlugin):
        raise PluginError(
            f"workflow plugin {ep.name!r} ({ep.value}) does not satisfy the WorkflowPlugin protocol"
        )
    plugin_id = obj.id
    if not isinstance(plugin_id, str) or not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise PluginError(f"workflow plugin {ep.name!r} has an invalid id: {plugin_id!r}")
    if plugin_id != ep.name:
        raise PluginError(
            f"workflow plugin id {plugin_id!r} does not match its entry point name {ep.name!r}"
        )
    return obj


def discover_plugins(group: str = ENTRY_POINT_GROUP) -> dict[str, WorkflowPlugin]:
    """Load every workflow plugin registered under ``group``, keyed by plugin id.

    Raises ``PluginError`` on any load or validation failure, including two
    distributions registering the same plugin id.
    """
    plugins: dict[str, WorkflowPlugin] = {}
    for ep in sorted(entry_points(group=group), key=lambda e: (e.name, e.value)):
        plugin = _load(ep)
        if plugin.id in plugins:
            raise PluginError(f"duplicate workflow plugin id {plugin.id!r}")
        plugins[plugin.id] = plugin
    return plugins

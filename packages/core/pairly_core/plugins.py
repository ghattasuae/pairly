# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Workflow plugin contract and discovery.

Workflows (Pairly Hire, Pairly Pay, and any third workflow) plug into the core engine
through the ``pairly.workflows`` Python entry point group. The core engine never imports
a workflow package directly; it only discovers plugin objects at runtime.

The ``WorkflowPlugin`` protocol is intentionally minimal for now. It grows with the
engine (entity types, models, extraction schemas, detectors, rules, policy schema,
approval types, templates, router, seed hook). ``graphs`` (WP1.8) lists the plugin's
LangGraph graphs; each ``GraphSpec`` carries its approval types and final actions.

Optional attributes, read with ``getattr`` so a plugin only declares what it uses:

* ``webhook_events``, ``feature_flags``, ``search_sources``, ``retention_rules``,
  ``scheduled_jobs``: see the module that builds each registry.
* ``api_router``: a callable with no arguments returning a FastAPI ``APIRouter``. The
  API mounts it under ``/v1/<plugin id>`` (``plugin_routers``). It is a callable so
  that processes without an HTTP API (the worker) never import the routes.
* ``demo_seeds``: a mapping of seed name (``<plugin id>-<name>``) to an async
  ``DemoSeed``, run by ``pairly-admin seed <name> --org <slug>`` (``demo_seeds``).
* ``migrations_dir``: the directory of the plugin's Alembic revisions, its own branch
  (``branch_labels``) that ``depends_on`` the core revision it needs. The Alembic
  environment adds it to the version locations (``plugin_migration_locations``).
* ``prompt_roots``: directories of the plugin's versioned prompt files
  (``<root>/<task>/<version>.md``, ``pairly_core.llm.prompt_registry``). Every task
  directory must be named ``<plugin id>_...``, so a plugin can never replace a core
  prompt or another plugin's. The model router loads them (``plugin_prompt_roots``).

Discovery fails closed: a plugin that cannot be loaded, does not satisfy the protocol,
has an invalid id, or collides with another plugin raises ``PluginError`` instead of
being silently skipped.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pairly_core.rbac import PermissionSpec, RoleRegistry
from pairly_core.workflow.spec import GraphSpec

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy.ext.asyncio import AsyncSession

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

    @property
    def permissions(self) -> Sequence[PermissionSpec]:
        """Permissions this workflow defines, all named ``<id>.<resource>.<action>``.

        Each spec lists the built-in roles that hold it by default. The ``agent`` role
        may only receive ``read`` and ``propose`` permissions.
        """
        ...

    @property
    def graphs(self) -> Sequence[GraphSpec]:
        """LangGraph graphs this workflow runs (``pairly_core.workflow.spec.GraphSpec``).

        Each spec belongs to this workflow and names permissions of it. Empty is fine.
        """
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
    _check_permissions(obj, ep)
    _check_graphs(obj, ep)
    return obj


def _check_permissions(obj: WorkflowPlugin, ep: EntryPoint) -> None:
    perms = obj.permissions
    if isinstance(perms, str | bytes) or not isinstance(perms, Sequence):
        raise PluginError(f"workflow plugin {ep.name!r}: permissions must be a sequence")
    for spec in perms:
        if not isinstance(spec, PermissionSpec):
            raise PluginError(f"workflow plugin {ep.name!r}: permissions must be PermissionSpec")
        if spec.namespace != obj.id:
            raise PluginError(
                f"workflow plugin {ep.name!r}: permission {spec.name!r} is outside its namespace"
            )


def _check_graphs(obj: WorkflowPlugin, ep: EntryPoint) -> None:
    graphs = obj.graphs
    if isinstance(graphs, str | bytes) or not isinstance(graphs, Sequence):
        raise PluginError(f"workflow plugin {ep.name!r}: graphs must be a sequence")
    names: set[str] = set()
    for spec in graphs:
        if not isinstance(spec, GraphSpec):
            raise PluginError(f"workflow plugin {ep.name!r}: graphs must be GraphSpec")
        if spec.workflow != obj.id:
            raise PluginError(
                f"workflow plugin {ep.name!r}: graph {spec.name!r} belongs to another workflow"
            )
        if spec.name in names:
            raise PluginError(f"workflow plugin {ep.name!r}: duplicate graph {spec.name!r}")
        names.add(spec.name)


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


# ------------------------------------------------------------------ optional: API routes


def plugin_routers(plugins: Iterable[Any]) -> list[tuple[str, APIRouter]]:
    """``(plugin id, router)`` for every plugin that declares ``api_router``.

    The API mounts each router under ``/v1/<plugin id>``, so a workflow's routes can
    never shadow a core route or another workflow's. Fails closed: a declared
    ``api_router`` that is not callable or does not return an ``APIRouter`` raises
    ``PluginError``.
    """
    from fastapi import APIRouter

    routers: list[tuple[str, APIRouter]] = []
    for plugin in plugins:
        factory = getattr(plugin, "api_router", None)
        if factory is None:
            continue
        if not callable(factory):
            raise PluginError(f"workflow plugin {plugin.id!r}: api_router must be callable")
        router = factory()
        if not isinstance(router, APIRouter):
            raise PluginError(f"workflow plugin {plugin.id!r}: api_router must return APIRouter")
        routers.append((plugin.id, router))
    return routers


# ------------------------------------------------------------------ optional: demo seeds

_SEED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}-[a-z][a-z0-9-]{0,47}$")


@dataclass(frozen=True, slots=True)
class SeedContext:
    """What a demo seed gets: the organisation (the session is bound to it), the role
    registry and the public web URL (for links it prints)."""

    org_id: uuid.UUID
    org_slug: str
    registry: RoleRegistry
    web_url: str


@dataclass(frozen=True, slots=True)
class SeededInvitation:
    email: str
    role: str
    workflow: str | None
    # Shown once by the CLI as an accept link; only its hash is stored.
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SeedResult:
    lines: tuple[str, ...] = ()
    invitations: tuple[SeededInvitation, ...] = ()


# Runs as the owner database role, in one transaction the CLI commits. Must be
# idempotent: running a seed twice changes nothing the first run already did.
DemoSeed = Callable[["AsyncSession", SeedContext], Awaitable[SeedResult]]


def demo_seeds(plugins: Iterable[Any]) -> dict[str, DemoSeed]:
    """Every plugin's ``demo_seeds``, by name. Names are ``<plugin id>-<name>``."""
    seeds: dict[str, DemoSeed] = {}
    for plugin in plugins:
        declared = getattr(plugin, "demo_seeds", None) or {}
        for name, seed in dict(declared).items():
            if not isinstance(name, str) or not _SEED_NAME_RE.fullmatch(name):
                raise PluginError(f"workflow plugin {plugin.id!r}: invalid seed name {name!r}")
            if not name.startswith(f"{plugin.id}-"):
                raise PluginError(
                    f"workflow plugin {plugin.id!r}: seed {name!r} is outside its namespace"
                )
            if not callable(seed):
                raise PluginError(f"workflow plugin {plugin.id!r}: seed {name!r} is not callable")
            if name in seeds:
                raise PluginError(f"duplicate demo seed {name!r}")
            seeds[name] = seed
    return seeds


# ------------------------------------------------------------------ optional: migrations


def plugin_migration_locations(plugins: Iterable[Any]) -> list[str]:
    """The Alembic version directories plugins declare (``migrations_dir``), sorted by
    plugin id. Fails closed: a declared directory that does not exist raises."""
    locations: list[str] = []
    for plugin in sorted(plugins, key=lambda p: str(p.id)):
        declared = getattr(plugin, "migrations_dir", None)
        if declared is None:
            continue
        path = Path(declared)
        if not path.is_dir():
            raise PluginError(f"workflow plugin {plugin.id!r}: migrations_dir is not a directory")
        locations.append(str(path.resolve()))
    return locations


# ------------------------------------------------------------------ optional: prompts


def plugin_prompt_roots(plugins: Iterable[Any]) -> list[Path]:
    """The prompt directories plugins declare (``prompt_roots``), sorted by plugin id.

    Fails closed: a declared root that is not a directory, or that holds a task directory
    outside the plugin's namespace (``<plugin id>_...``), raises ``PluginError``.
    """
    roots: list[Path] = []
    for plugin in sorted(plugins, key=lambda p: str(p.id)):
        declared = getattr(plugin, "prompt_roots", None) or ()
        if isinstance(declared, str | bytes | Path):
            raise PluginError(f"workflow plugin {plugin.id!r}: prompt_roots must be a sequence")
        for item in declared:
            path = Path(item)
            if not path.is_dir():
                raise PluginError(f"workflow plugin {plugin.id!r}: prompt root is not a directory")
            for task in sorted(p.name for p in path.iterdir() if p.is_dir()):
                if task.startswith(("_", ".")):
                    continue
                if not task.startswith(f"{plugin.id}_"):
                    raise PluginError(
                        f"workflow plugin {plugin.id!r}: prompt task {task!r} is outside its "
                        "namespace"
                    )
            roots.append(path.resolve())
    return roots

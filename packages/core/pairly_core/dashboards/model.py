# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The shape of the shared and system dashboards (brief 5.1 items 12 to 15).

A board is a list of sections. A section has a count (or ``None`` when there is no data:
never a made-up zero), the app path that lists its records, a few items (each with the
app path of its record) and a few stats. ``source`` says where the numbers come from
when that is not obvious (a file, an in-memory counter); ``note`` is a machine key
(``not_configured``, ``not_recorded``, ``no_data``, ...) the web app translates.

Workflow plugins add sections through an optional ``dashboard_sections`` attribute: a
sequence of ``DashboardSectionSpec``. Each names its board, the permission the viewer
must be able to use, and a coroutine that builds the section in the viewer's
tenant-bound session. Core never imports a workflow: the specs are read from the
discovered plugin objects (``build_section_registry``).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.rbac import Actor, PermissionDeniedError, authorize

log = structlog.get_logger("pairly_core.dashboards")

MAX_ITEMS: Final = 10
KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

Scalar = str | int | float | bool | None


class Board(StrEnum):
    AUDITOR = "auditor"
    ADMIN = "admin"
    AI_GOVERNANCE = "ai_governance"
    PLATFORM_OPS = "platform_ops"


class Unit(StrEnum):
    COUNT = "count"
    RATIO = "ratio"
    USD = "usd"
    MS = "ms"
    BYTES = "bytes"
    SECONDS = "seconds"
    TEXT = "text"


@dataclass(slots=True)
class Item:
    kind: str
    id: str
    label: str
    href: str | None = None
    at: datetime | None = None
    status: str | None = None
    detail: dict[str, Scalar] = field(default_factory=dict)


@dataclass(slots=True)
class Stat:
    """One number. ``value`` None means no data (shown as such, never as 0)."""

    key: str
    value: float | None
    unit: Unit = Unit.COUNT
    href: str | None = None
    text: str | None = None
    # Of how many (the denominator of a ratio, or the sample behind a number).
    n: int | None = None


@dataclass(slots=True)
class Section:
    key: str
    count: int | None
    href: str | None = None
    items: list[Item] = field(default_factory=list)
    stats: list[Stat] = field(default_factory=list)
    source: str | None = None
    note: str | None = None
    workflow: str | None = None


@dataclass(slots=True)
class Link:
    key: str
    href: str


@dataclass(slots=True)
class Dashboard:
    board: Board
    generated_at: datetime
    action: list[Section] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


def section(
    key: str,
    items: Sequence[Item],
    *,
    count: int | None = None,
    href: str | None = None,
    **kwargs: Any,
) -> Section:
    """A section listing at most ``MAX_ITEMS`` of ``items``; ``count`` defaults to all."""
    return Section(
        key=key,
        count=len(items) if count is None else count,
        href=href,
        items=list(items[:MAX_ITEMS]),
        **kwargs,
    )


def may(actor: Actor, permission: str) -> bool:
    """Whether ``authorize`` would let ``actor`` use ``permission`` (second factor
    included), without raising or auditing a denial: a dashboard leaves out what the
    viewer may not see, it does not refuse the whole board."""
    try:
        authorize(actor, permission)
    except PermissionDeniedError:
        return False
    return True


# ------------------------------------------------------------------ plugin sections


@dataclass(frozen=True, slots=True)
class SectionContext:
    session: AsyncSession
    actor: Actor
    now: datetime


SectionBuilder = Callable[[SectionContext], Awaitable[Section | None]]


class DashboardSectionError(ValueError):
    """An invalid plugin dashboard section (a programming error)."""


@dataclass(frozen=True, slots=True)
class DashboardSectionSpec:
    board: Board
    key: str
    permission: str
    build: SectionBuilder
    # Where it goes: the "action needed" group at the top, or the rest.
    action: bool = False

    def __post_init__(self) -> None:
        if not KEY_RE.fullmatch(self.key):
            raise DashboardSectionError(f"invalid dashboard section key {self.key!r}")
        if self.board is Board.PLATFORM_OPS:
            # Platform operations never show tenant data, so no plugin adds to it.
            raise DashboardSectionError("plugins cannot add sections to platform operations")


class SectionRegistry:
    def __init__(self, specs: Iterable[tuple[str, DashboardSectionSpec]] = ()) -> None:
        self._specs: list[tuple[str, DashboardSectionSpec]] = []
        seen: set[tuple[Board, str]] = set()
        for workflow, spec in specs:
            if (spec.board, spec.key) in seen:
                raise DashboardSectionError(f"duplicate dashboard section {spec.key!r}")
            seen.add((spec.board, spec.key))
            self._specs.append((workflow, spec))

    def for_board(self, board: Board) -> list[tuple[str, DashboardSectionSpec]]:
        return [(w, s) for w, s in self._specs if s.board is board]


def build_section_registry(plugins: Iterable[Any] = ()) -> SectionRegistry:
    """Every plugin's optional ``dashboard_sections``; a section's permission must be
    the plugin's own (``<plugin id>.``)."""
    specs: list[tuple[str, DashboardSectionSpec]] = []
    for plugin in plugins:
        for spec in getattr(plugin, "dashboard_sections", ()) or ():
            if not isinstance(spec, DashboardSectionSpec):
                raise DashboardSectionError(
                    f"plugin {plugin.id!r}: dashboard_sections must be DashboardSectionSpec"
                )
            if not spec.permission.startswith(f"{plugin.id}."):
                raise DashboardSectionError(
                    f"plugin {plugin.id!r}: section {spec.key!r} must use its own permission"
                )
            specs.append((str(plugin.id), spec))
    return SectionRegistry(specs)


async def plugin_sections(
    registry: SectionRegistry, board: Board, ctx: SectionContext
) -> tuple[list[Section], list[Section]]:
    """(action, rest) sections the plugins add to ``board`` that the viewer may see."""
    action: list[Section] = []
    rest: list[Section] = []
    for workflow, spec in registry.for_board(board):
        if not may(ctx.actor, spec.permission):
            continue
        try:
            # A savepoint: one failing section never aborts the board's transaction.
            async with ctx.session.begin_nested():
                built = await spec.build(ctx)
        except Exception as exc:
            log.error("dashboard_section_failed", key=spec.key, error=type(exc).__name__)
            built = Section(key=spec.key, count=None, note="unavailable")
        if built is None:
            continue
        built.workflow = built.workflow or workflow
        (action if spec.action else rest).append(built)
    return action, rest

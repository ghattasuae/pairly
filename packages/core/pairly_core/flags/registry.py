# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Declared feature flags: core plus every workflow plugin's ``feature_flags``.

The API and the worker install the registry at startup (``install_flag_registry``),
like the webhook event registry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

FLAG_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")

# Outbound webhooks are delivered. While off, events still go to the outbox and nothing
# is sent; switching back on delivers them (a delivery is retried for 24 hours from its
# event, so events older than that get one attempt).
WEBHOOKS_OUTBOUND: Final = "webhooks.outbound"
# Parsed document text is indexed for search (PII redacted first). Off by default: it
# copies document text into the search index and costs an embedding call per document.
SEARCH_DOCUMENTS: Final = "search.documents"
# Hosted demo only: visitors may sign in as the organisation's seeded demo users without
# an email (``pairly_core.demo_signin``). Off by default, and it also needs the
# deployment switch ``PAIRLY_DEMO_PUBLIC_SIGNIN`` and a non-production environment.
DEMO_PUBLIC_SIGNIN: Final = "demo.public_signin"


class FlagError(ValueError):
    """An invalid or unknown flag (a programming error, or a bad request at the API)."""


@dataclass(frozen=True, slots=True)
class FlagSpec:
    key: str
    description: str
    default: bool = False

    def __post_init__(self) -> None:
        if not FLAG_KEY_RE.fullmatch(self.key) or len(self.key) > 100:
            raise FlagError(f"invalid flag key {self.key!r}")
        if not isinstance(self.default, bool):
            raise FlagError(f"flag {self.key!r}: default must be a bool")


CORE_FLAGS: tuple[FlagSpec, ...] = (
    FlagSpec(WEBHOOKS_OUTBOUND, "Deliver outbound webhooks", default=True),
    FlagSpec(SEARCH_DOCUMENTS, "Index parsed documents for search (PII redacted)"),
    FlagSpec(DEMO_PUBLIC_SIGNIN, "Hosted demo: visitors sign in as the seeded demo users"),
)


class FlagRegistry:
    def __init__(self, flags: Iterable[FlagSpec] = CORE_FLAGS) -> None:
        self._flags: dict[str, FlagSpec] = {}
        for flag in flags:
            self.register(flag)

    def register(self, flag: FlagSpec) -> None:
        if flag.key in self._flags:
            raise FlagError(f"duplicate flag {flag.key!r}")
        self._flags[flag.key] = flag

    def __contains__(self, key: object) -> bool:
        return key in self._flags

    @property
    def flags(self) -> list[FlagSpec]:
        return sorted(self._flags.values(), key=lambda f: f.key)

    def require(self, key: str) -> FlagSpec:
        try:
            return self._flags[key]
        except KeyError:
            raise FlagError(f"unknown flag {key!r}") from None


def build_flag_registry(plugins: Iterable[Any] = ()) -> FlagRegistry:
    """Core flags plus every plugin's optional ``feature_flags`` (in its namespace)."""
    registry = FlagRegistry()
    for plugin in plugins:
        for flag in getattr(plugin, "feature_flags", ()) or ():
            if not isinstance(flag, FlagSpec):
                raise FlagError(f"plugin {plugin.id!r}: feature_flags must be FlagSpec")
            if not flag.key.startswith(f"{plugin.id}."):
                raise FlagError(f"plugin {plugin.id!r}: flag {flag.key!r} is outside its namespace")
            registry.register(flag)
    return registry


_registry = FlagRegistry()


def install_flag_registry(registry: FlagRegistry) -> None:
    global _registry
    _registry = registry


def flag_registry() -> FlagRegistry:
    return _registry

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Structural diff of two JSON values, used by replay to flag differences."""

from __future__ import annotations

from typing import Any

from pairly_core.llm.types import DiffEntry

_MISSING = "<missing>"


def json_diff(original: Any, replayed: Any, path: str = "$") -> list[DiffEntry]:
    """Every leaf path where the two values differ. Lists compare by position.

    ``1`` and ``1.0`` are equal; ``True`` and ``1`` are not.
    """
    if isinstance(original, dict) and isinstance(replayed, dict):
        out: list[DiffEntry] = []
        for key in sorted(set(original) | set(replayed), key=str):
            child = f"{path}.{key}"
            if key not in original:
                out.append(DiffEntry(child, _MISSING, replayed[key]))
            elif key not in replayed:
                out.append(DiffEntry(child, original[key], _MISSING))
            else:
                out.extend(json_diff(original[key], replayed[key], child))
        return out
    if isinstance(original, list) and isinstance(replayed, list):
        out = []
        for i in range(max(len(original), len(replayed))):
            child = f"{path}[{i}]"
            if i >= len(original):
                out.append(DiffEntry(child, _MISSING, replayed[i]))
            elif i >= len(replayed):
                out.append(DiffEntry(child, original[i], _MISSING))
            else:
                out.extend(json_diff(original[i], replayed[i], child))
        return out
    if isinstance(original, bool) != isinstance(replayed, bool) or original != replayed:
        return [DiffEntry(path, original, replayed)]
    return []

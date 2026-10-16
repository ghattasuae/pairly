# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Read an encrypted column whether it was loaded from the database (``Sealed``) or set
in this transaction and not reloaded yet (``Plaintext`` from ``seal``)."""

from __future__ import annotations

from typing import Any

from pairly_core.crypto.types import Plaintext, unseal


def read_sealed(row: Any, column: str) -> Any:
    value = getattr(row, column)
    if value is None:
        return None
    if isinstance(value, Plaintext):
        return value.value
    return unseal(row, column)

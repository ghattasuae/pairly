# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""A minimal SQLAlchemy type for pgvector's ``vector(n)``.

Values cross the wire as pgvector's text form (``[0.1,0.2,...]``) with explicit casts on
both sides, so no driver-level codec is needed and the dependency set stays small.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, Text, cast, type_coerce
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType

# Width of ``search_chunks.embedding``: every embedder emits this many values.
DIMENSIONS = 1536


def to_pgvector(values: Sequence[float]) -> str:
    """pgvector text literal. Rejects NaN and infinity (they would poison distances)."""
    parts: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        parts.append(repr(number))
    return "[" + ",".join(parts) + "]"


def from_pgvector(text: str) -> list[float]:
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ValueError("not a pgvector literal")
    inner = body[1:-1].strip()
    return [float(p) for p in inner.split(",")] if inner else []


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        if not 1 <= dimensions <= 16000:
            raise ValueError("dimensions must be 1..16000")
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: Any) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect: Any) -> Any:
        dims = self.dimensions

        def process(value: Sequence[float] | None) -> str | None:
            if value is None:
                return None
            if len(value) != dims:
                raise ValueError(f"expected a vector of {dims} values, got {len(value)}")
            return to_pgvector(value)

        return process

    def bind_expression(self, bindvalue: Any) -> ColumnElement[Any]:
        return cast(cast(bindvalue, String), self)

    def column_expression(self, colexpr: Any) -> ColumnElement[Any]:
        return type_coerce(cast(colexpr, Text), self)

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else from_pgvector(value)

        return process

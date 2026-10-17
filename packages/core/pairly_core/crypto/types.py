# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""SQLAlchemy column types for encrypted fields.

A model declares an encrypted column explicitly:

    request: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    sender_address: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)

Writing needs the row context, because the ciphertext is bound to organisation, table,
column and row id. A column accepts only:

* ``seal(Model, "column", value, org_id=..., row_id=...)``: a ``Plaintext`` that is
  encrypted when the statement is bound, or
* a ``Sealed`` read back from the database (copied as is, for the same row).

Anything else (a bare string or dict) raises ``TypeError``, so plaintext can never be
written to an encrypted column by mistake. Reading gives a ``Sealed``; decrypt it with
``unseal(row, "column")`` (uses the row's ``org_id`` and ``id``) or ``Sealed.open_*``.

Every table with an encrypted column must have ``id`` and ``org_id`` columns.
``encrypted_columns(metadata)`` lists them for key rotation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import MetaData, Table, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from pairly_core.crypto.envelope import Aad, decrypt, encrypt, kek_version
from pairly_core.crypto.keys import KeyRing


class Sealed:
    """A ciphertext token read from (or bound for) an encrypted column."""

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        if not isinstance(token, str):
            raise TypeError("Sealed wraps a ciphertext token string")
        self.token = token

    def __repr__(self) -> str:
        return "Sealed(<ciphertext>)"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Sealed) and other.token == self.token

    def __hash__(self) -> int:
        return hash(self.token)

    @property
    def kek_version(self) -> str:
        return kek_version(self.token)

    def open_bytes(self, aad: Aad, keyring: KeyRing | None = None) -> bytes:
        return decrypt(self.token, aad, keyring)

    def open_text(self, aad: Aad, keyring: KeyRing | None = None) -> str:
        return self.open_bytes(aad, keyring).decode("utf-8")

    def open_json(self, aad: Aad, keyring: KeyRing | None = None) -> Any:
        return json.loads(self.open_bytes(aad, keyring))


@dataclass(frozen=True, slots=True)
class Plaintext:
    """A value to encrypt for ``aad`` when the statement is bound. Never printed."""

    value: Any
    aad: Aad
    keyring: KeyRing | None = None

    def __repr__(self) -> str:
        return f"Plaintext(<hidden>, table={self.aad.table!r}, column={self.aad.column!r})"


class _EncryptedType(TypeDecorator[Any]):
    impl = Text
    cache_ok = True
    kind = "bytes"

    def encode(self, value: Any) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def decode(self, data: bytes) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, Sealed):
            return value.token
        if isinstance(value, Plaintext):
            if value.value is None:
                return None
            return encrypt(self.encode(value.value), value.aad, value.keyring)
        raise TypeError(
            "encrypted column: pass seal(Model, column, value, org_id=..., row_id=...) or a "
            f"Sealed value, not {type(value).__name__} (refusing to write plaintext)"
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> Sealed | None:
        if value is None:
            return None
        return Sealed(str(value))


class EncryptedString(_EncryptedType):
    """Encrypted UTF-8 text."""

    cache_ok = True
    kind = "string"

    def encode(self, value: Any) -> bytes:
        if not isinstance(value, str):
            raise TypeError("EncryptedString stores str values")
        return value.encode("utf-8")

    def decode(self, data: bytes) -> str:
        return data.decode("utf-8")


class EncryptedJSON(_EncryptedType):
    """Encrypted JSON (any JSON-serialisable value; key order is normalised)."""

    cache_ok = True
    kind = "json"

    def encode(self, value: Any) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def decode(self, data: bytes) -> Any:
        return json.loads(data)


def _table_name(model: Any) -> str:
    if isinstance(model, Table):
        return model.name
    name = getattr(model, "__tablename__", None)
    if not isinstance(name, str):
        raise TypeError("expected a mapped model class or a Table")
    return name


def _column_type(model: Any, column: str) -> _EncryptedType:
    table: Table | None = model if isinstance(model, Table) else getattr(model, "__table__", None)
    if table is None or column not in table.c:
        raise ValueError(f"{_table_name(model)} has no column {column!r}")
    col_type = table.c[column].type
    if not isinstance(col_type, _EncryptedType):
        raise ValueError(f"{table.name}.{column} is not an encrypted column")
    return col_type


def field_aad(model: Any, column: str, *, org_id: uuid.UUID, row_id: object) -> Aad:
    """The AAD for ``model.column`` of one row (checks that the column is encrypted)."""
    _column_type(model, column)
    return Aad.for_row(org_id, _table_name(model), column, row_id)


def seal(
    model: Any,
    column: str,
    value: Any,
    *,
    org_id: uuid.UUID,
    row_id: object,
    keyring: KeyRing | None = None,
) -> Plaintext:
    """A value ready to be written to ``model.column`` of the row ``row_id``."""
    return Plaintext(value, field_aad(model, column, org_id=org_id, row_id=row_id), keyring)


def unseal(row: Any, column: str, keyring: KeyRing | None = None) -> Any:
    """Decrypt ``row.column`` with the row's own ``org_id`` and ``id``. ``None`` stays None."""
    sealed = getattr(row, column)
    if sealed is None:
        return None
    if not isinstance(sealed, Sealed):
        raise TypeError(f"{column} is not a sealed value")
    model = type(row)
    col_type = _column_type(model, column)
    aad = Aad.for_row(row.org_id, _table_name(model), column, row.id)
    return col_type.decode(sealed.open_bytes(aad, keyring))


@dataclass(frozen=True, slots=True)
class EncryptedColumn:
    table: Table
    column: str
    kind: str


def encrypted_columns(metadata: MetaData) -> Iterator[EncryptedColumn]:
    """Every encrypted column in ``metadata``, sorted by table and column name."""
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        for col in sorted(table.columns, key=lambda c: c.name):
            if isinstance(col.type, _EncryptedType):
                if "id" not in table.c or "org_id" not in table.c:
                    raise ValueError(f"{table.name} has encrypted columns but no id or org_id")
                yield EncryptedColumn(table, col.name, col.type.kind)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Compile an extraction schema (a Pydantic ``ExtractionModel``) into field specs.

Paths name fields in model output, storage and corrections: ``invoice_number``,
``bank.iban``, ``line_items[0].quantity``. The schema pattern of a path replaces every
index with ``[]`` (``line_items[].quantity``).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, TypeGuard, get_args, get_origin

from pydantic.fields import FieldInfo

from pairly_core.extraction.types import Extracted, ExtractionModel, Money, Percent, Rule
from pairly_core.extraction.validators import FieldKind, ValueSpec, shape_for

MAX_DEPTH = 4
_SEGMENT = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
PATH_RE = re.compile(
    rf"^{_SEGMENT}(\[\d{{1,4}}\])?(\.{_SEGMENT}(\[\d{{1,4}}\])?){{0,{MAX_DEPTH - 1}}}$"
)
_INDEX_RE = re.compile(r"\[(\d{1,4})\]")
_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_.]{0,63}$")
_SCHEMA_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


class SchemaError(TypeError):
    """An extraction schema uses an unsupported type or is malformed (a programming error)."""


def path_pattern(path: str) -> str:
    return _INDEX_RE.sub("[]", path)


def path_indices(path: str) -> list[int]:
    return [int(i) for i in _INDEX_RE.findall(path)]


@dataclass(frozen=True, slots=True)
class LeafSpec:
    name: str
    pattern: str
    value: ValueSpec
    required: bool
    threshold: float
    description: str | None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.pattern, "required": self.required}
        out.update(self.value.describe())
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True, slots=True)
class GroupSpec:
    name: str
    pattern: str
    model: type[ExtractionModel]
    fields: tuple[Node, ...]
    is_list: bool
    required: bool
    max_items: int
    description: str | None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.pattern + ("[]" if self.is_list else ""),
            "type": "list" if self.is_list else "group",
            "required": self.required,
            "fields": [f.describe() for f in self.fields],
        }
        if self.is_list:
            out["max_items"] = self.max_items
        if self.description:
            out["description"] = self.description
        return out


Node = LeafSpec | GroupSpec


@dataclass(frozen=True, slots=True)
class CompiledSchema:
    model: type[ExtractionModel]
    name: str
    version: str
    fields: tuple[Node, ...]
    leaves: dict[str, LeafSpec]
    groups: dict[str, GroupSpec]

    def describe(self) -> dict[str, Any]:
        """The schema description the extraction prompt receives."""
        out: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "fields": [f.describe() for f in self.fields],
        }
        if self.model.schema_description:
            out["description"] = self.model.schema_description
        return out

    def field_specs(self) -> dict[str, Any]:
        """Stored with each extraction: the rules corrections are checked against."""
        return {
            "leaves": {
                pattern: {
                    "value": leaf.value.to_json(),
                    "required": leaf.required,
                    "threshold": leaf.threshold,
                }
                for pattern, leaf in self.leaves.items()
            },
            "lists": {p: g.max_items for p, g in self.groups.items() if g.is_list},
        }

    def leaf_for(self, path: str) -> LeafSpec | None:
        """The leaf a concrete path points to, with every list index within bounds."""
        if not PATH_RE.fullmatch(path):
            return None
        leaf = self.leaves.get(path_pattern(path))
        if leaf is None:
            return None
        # Check each index against its list's max_items.
        prefix = ""
        for part in path.split("."):
            name, _, rest = part.partition("[")
            prefix = f"{prefix}.{name}" if prefix else name
            if rest:
                group = self.groups.get(path_pattern(prefix))
                if group is None or not group.is_list or int(rest[:-1]) >= group.max_items:
                    return None
                prefix += "[]"
        return leaf


def _rule(info: FieldInfo) -> Rule:
    for item in info.metadata:
        if isinstance(item, Rule):
            return item
    return Rule()


def _extracted_arg(annotation: Any) -> Any | None:
    meta = getattr(annotation, "__pydantic_generic_metadata__", None)
    if isinstance(meta, dict) and meta.get("origin") is Extracted and meta.get("args"):
        return meta["args"][0]
    return None


def _value_spec(type_: Any, rule: Rule, where: str) -> ValueSpec:
    shape = shape_for(type_)
    if shape is not None:
        return ValueSpec(FieldKind.SHAPE, rule, shape=shape)
    if type_ is Percent:
        return ValueSpec(FieldKind.PERCENT, rule)
    if get_origin(type_) is Literal:
        choices = get_args(type_)
        if not choices or not all(isinstance(c, str) for c in choices):
            raise SchemaError(f"{where}: Literal choices must be strings")
        return ValueSpec(FieldKind.ENUM, rule, choices=tuple(choices))
    if isinstance(type_, type):
        if issubclass(type_, StrEnum):
            return ValueSpec(
                FieldKind.ENUM, rule, choices=tuple(m.value for m in type_), enum_type=type_
            )
        if issubclass(type_, bool) or issubclass(type_, datetime):
            raise SchemaError(f"{where}: unsupported value type {type_.__name__}")
        simple = {
            str: FieldKind.TEXT,
            int: FieldKind.INTEGER,
            Decimal: FieldKind.DECIMAL,
            date: FieldKind.DATE,
            Money: FieldKind.MONEY,
        }
        kind = simple.get(type_)
        if kind is not None:
            return ValueSpec(kind, rule)
    raise SchemaError(f"{where}: unsupported value type {type_!r}")


def _is_model(type_: Any) -> TypeGuard[type[ExtractionModel]]:
    return isinstance(type_, type) and issubclass(type_, ExtractionModel)


def _compile_fields(
    model: type[ExtractionModel],
    prefix: str,
    depth: int,
    leaves: dict[str, LeafSpec],
    groups: dict[str, GroupSpec],
) -> tuple[Node, ...]:
    if depth > MAX_DEPTH:
        raise SchemaError(f"{model.__name__}: nested deeper than {MAX_DEPTH} levels")
    nodes: list[Node] = []
    for name, info in model.model_fields.items():
        if not re.fullmatch(_SEGMENT, name):
            raise SchemaError(f"{model.__name__}.{name}: invalid field name")
        pattern = f"{prefix}.{name}" if prefix else name
        rule = _rule(info)
        description = rule.description or info.description
        annotation = info.annotation
        arg = _extracted_arg(annotation)
        if arg is not None:
            leaf = LeafSpec(
                name=name,
                pattern=pattern,
                value=_value_spec(arg, rule, pattern),
                required=rule.required,
                threshold=rule.threshold if rule.threshold is not None else model.default_threshold,
                description=description,
            )
            leaves[pattern] = leaf
            nodes.append(leaf)
            continue
        is_list = get_origin(annotation) is list
        inner = get_args(annotation)[0] if is_list and get_args(annotation) else annotation
        if not _is_model(inner):
            raise SchemaError(
                f"{pattern}: fields must be Extracted[T], an ExtractionModel or a list of them"
            )
        child_prefix = pattern + ("[]" if is_list else "")
        group = GroupSpec(
            name=name,
            pattern=pattern,
            model=inner,
            fields=_compile_fields(inner, child_prefix, depth + 1, leaves, groups),
            is_list=is_list,
            required=rule.required,
            max_items=rule.max_items,
            description=description,
        )
        groups[pattern] = group
        nodes.append(group)
    return tuple(nodes)


@functools.cache
def compile_schema(model: type[ExtractionModel]) -> CompiledSchema:
    """Validate and compile ``model``. Cached per class."""
    if not _is_model(model):
        raise SchemaError("an extraction schema must subclass ExtractionModel")
    if not _SCHEMA_NAME_RE.fullmatch(model.schema_name):
        raise SchemaError(f"{model.__name__}: schema_name must look like '<workflow>.<name>'")
    if not _SCHEMA_VERSION_RE.fullmatch(model.schema_version):
        raise SchemaError(f"{model.__name__}: schema_version is required")
    if not 0 <= model.default_threshold <= 1:
        raise SchemaError(f"{model.__name__}: default_threshold must be within [0, 1]")
    leaves: dict[str, LeafSpec] = {}
    groups: dict[str, GroupSpec] = {}
    fields = _compile_fields(model, "", 1, leaves, groups)
    if not leaves:
        raise SchemaError(f"{model.__name__}: no fields")
    return CompiledSchema(
        model=model,
        name=model.schema_name,
        version=model.schema_version,
        fields=fields,
        leaves=leaves,
        groups=groups,
    )


def schema_workflow(schema: CompiledSchema) -> str:
    return schema.name.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class StoredSpecs:
    """Field specs read back from an extraction row (``CompiledSchema.field_specs``)."""

    leaves: dict[str, Mapping[str, Any]]
    lists: dict[str, int]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> StoredSpecs:
        return cls(leaves=dict(data.get("leaves", {})), lists=dict(data.get("lists", {})))

    def value_spec(self, path: str) -> tuple[str, ValueSpec] | None:
        """The pattern and value spec of a concrete path, or None if it is not valid."""
        if not PATH_RE.fullmatch(path):
            return None
        pattern = path_pattern(path)
        leaf = self.leaves.get(pattern)
        if leaf is None:
            return None
        prefix = ""
        for part in path.split("."):
            name, _, rest = part.partition("[")
            prefix = f"{prefix}.{name}" if prefix else name
            if rest:
                limit = self.lists.get(path_pattern(prefix))
                if limit is None or int(rest[:-1]) >= int(limit):
                    return None
                prefix += "[]"
        try:
            return pattern, ValueSpec.from_json(leaf["value"])
        except (KeyError, ValueError, TypeError):
            return None

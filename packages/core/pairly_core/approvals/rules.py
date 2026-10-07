# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Approval rules: policy as code (YAML validated by Pydantic), stored in ``policy_versions``.

One rules document per approval type (``kind`` ``approval_rules``, ``name`` the approval
type key). Example::

    schema: pairly.approval_rules/v1
    approval_type: pay.invoice
    sla_hours: 24          # default time a step may stay open
    warn_hours: 4          # "due soon" this long before the deadline
    escalation:
      roles: [finance_controller]
    chains:
      - name: small
        when:
          - {field: amount, op: lte, value: "1000"}
        steps:
          - {name: manager, mode: any_of, roles: [approver]}
      - name: large        # no "when": the default chain, must come last
        steps:
          - {name: manager, mode: any_of, roles: [approver]}
          - {name: finance, mode: all_of, roles: [finance_controller], sla_hours: 48}

Chain selection: the first chain whose conditions all hold for the approval payload.
Conditions read a field of the payload (dotted path). Number comparisons use ``Decimal``
(write amounts as strings to keep them exact). A missing field or a value that is not a
number makes a numeric condition false. If no chain matches, the request fails closed
(``NoMatchingChainError``): nothing is ever approved by default.

Steps run in order. ``any_of``: one approval from the step's approvers completes it.
``all_of``: every approver resolved for the step must approve. Approvers come from
``roles`` (members holding one of the roles in the approval's workflow) and ``users``
(explicit user ids); either way they must hold the approval type's permission.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pairly_core.policy_versions import PolicyError

RULES_KIND = "approval_rules"
SCHEMA_ID = "pairly.approval_rules/v1"
MAX_SLA_HOURS = 24 * 30

_FIELD_PATTERN = r"^[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_]{0,63}){0,3}$"
_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_ROLE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

Scalar = str | int | bool
Name = Annotated[str, Field(pattern=_NAME_PATTERN)]
RoleKey = Annotated[str, Field(pattern=_ROLE_PATTERN)]

_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte"})


class NoMatchingChainError(PolicyError):
    """No chain of the rules applies to this approval (fail closed)."""


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def lookup(facts: Mapping[str, Any], path: str) -> object:
    """Dotted path lookup. Returns ``None`` when any part is missing."""
    value: object = facts
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Condition(_Model):
    field: Annotated[str, Field(pattern=_FIELD_PATTERN)]
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"]
    value: Scalar | Annotated[list[Scalar], Field(min_length=1, max_length=100)]

    @field_validator("value", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        # YAML turns 1000.50 into a float; keep the written digits instead.
        if isinstance(value, float):
            return str(value)
        if isinstance(value, list):
            return [str(v) if isinstance(v, float) else v for v in value]
        return value

    @model_validator(mode="after")
    def _shape(self) -> Condition:
        is_list = isinstance(self.value, list)
        if self.op in {"in", "not_in"} and not is_list:
            raise ValueError(f"{self.op} needs a list value")
        if self.op not in {"in", "not_in"} and is_list:
            raise ValueError(f"{self.op} needs a single value")
        if self.op in _NUMERIC_OPS and _as_decimal(self.value) is None:
            raise ValueError(f"{self.op} needs a number")
        return self

    def holds(self, facts: Mapping[str, Any]) -> bool:
        actual = lookup(facts, self.field)
        if actual is None:
            return False
        if self.op in _NUMERIC_OPS:
            left, right = _as_decimal(actual), _as_decimal(self.value)
            if left is None or right is None:
                return False
            return {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
            }[self.op]
        if self.op in {"in", "not_in"}:
            assert isinstance(self.value, list)  # noqa: S101 (checked by _shape)
            found = any(_equal(actual, v) for v in self.value)
            return found if self.op == "in" else not found
        assert not isinstance(self.value, list)  # noqa: S101 (checked by _shape)
        same = _equal(actual, self.value)
        return same if self.op == "eq" else not same


def _equal(actual: object, expected: Scalar) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    left, right = _as_decimal(actual), _as_decimal(expected)
    if left is not None and right is not None:
        return left == right
    return str(actual) == str(expected)


class StepRule(_Model):
    name: Name
    mode: Literal["any_of", "all_of"] = "any_of"
    roles: Annotated[list[RoleKey], Field(max_length=20)] = []
    users: Annotated[list[uuid.UUID], Field(max_length=50)] = []
    sla_hours: Annotated[int, Field(ge=1, le=MAX_SLA_HOURS)] | None = None

    @model_validator(mode="after")
    def _has_approvers(self) -> StepRule:
        if not self.roles and not self.users:
            raise ValueError(f"step {self.name!r} needs roles or users")
        return self


class ChainRule(_Model):
    name: Name
    when: Annotated[list[Condition], Field(max_length=20)] = []
    steps: Annotated[list[StepRule], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def _unique_steps(self) -> ChainRule:
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"chain {self.name!r} repeats a step name")
        return self

    def applies(self, facts: Mapping[str, Any]) -> bool:
        return all(c.holds(facts) for c in self.when)


class EscalationRule(_Model):
    roles: Annotated[list[RoleKey], Field(min_length=1, max_length=20)]


class ApprovalRules(_Model):
    schema_id: Literal["pairly.approval_rules/v1"] = Field(alias="schema")
    approval_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")]
    sla_hours: Annotated[int, Field(ge=1, le=MAX_SLA_HOURS)] = 24
    warn_hours: Annotated[int, Field(ge=0, le=MAX_SLA_HOURS)] = 4
    escalation: EscalationRule | None = None
    chains: Annotated[list[ChainRule], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def _chains(self) -> ApprovalRules:
        names = [c.name for c in self.chains]
        if len(names) != len(set(names)):
            raise ValueError("chain names must be unique")
        for chain in self.chains[:-1]:
            if not chain.when:
                raise ValueError(f"chain {chain.name!r} has no conditions, so it must be last")
        return self

    def select_chain(self, facts: Mapping[str, Any]) -> ChainRule:
        for chain in self.chains:
            if chain.applies(facts):
                return chain
        raise NoMatchingChainError("no approval chain matches this request")

    def step_sla_seconds(self, step: StepRule) -> int:
        return (step.sla_hours or self.sla_hours) * 3600

    def warn_seconds(self, sla_seconds: int) -> int:
        return min(self.warn_hours * 3600, sla_seconds)


def parse_rules(content: str) -> ApprovalRules:
    """Parse and validate a rules document. Raises ``PolicyError`` with a short reason."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        raise PolicyError("the approval rules are not valid YAML") from None
    if not isinstance(data, dict):
        raise PolicyError("the approval rules must be a mapping")
    try:
        return ApprovalRules.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'rules'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)[:10]
        )
        raise PolicyError(f"invalid approval rules: {problems}") from None


def rules_validator(approval_type: str) -> Any:
    """A ``validate`` callable for ``publish_policy_version``: valid and for this type."""

    def validate(content: str) -> ApprovalRules:
        rules = parse_rules(content)
        if rules.approval_type != approval_type:
            raise PolicyError("the rules are for another approval type")
        return rules

    return validate

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic policy rules: versioned YAML validated by Pydantic.

One rule set per policy name (``policy_versions`` kind ``rules``). Example::

    schema: pairly.rules/v1
    name: pay.spend
    facts:
      amount: decimal
      currency: string
      category: string
      po_number: {type: string, optional: true}
    rules:
      - id: software_it_approval
        description:
          en: Software over 10,000 AED needs IT approval
          ar: البرمجيات التي تزيد على 10,000 درهم تحتاج إلى موافقة قسم تقنية المعلومات
        when: "category == 'software' and currency == 'AED' and amount > 10000"
        outcome: needs_review      # when the condition holds
        otherwise: pass            # when it does not (default pass)
        severity: high
        reason:
          en: "Software purchase of {amount} {currency} needs IT approval"

Conditions use the safe expression language in ``pairly_core.policy.expr`` and are
compiled when the rule set is validated, so a rule set that does not parse or type
check is never stored. Reason templates may only reference declared facts
(``{amount}``); they are filled by plain substitution, never ``str.format``.

Evaluation is fail closed: a missing required fact, a malformed fact value or an
arithmetic error makes that rule's result ``error``, and ``error`` sends the item to a
person. ``error`` is never treated as ``pass``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from pairly_core.policy.expr import (
    FACT_PATH_RE,
    FACT_TYPES,
    Environment,
    EvalError,
    ExprError,
    Expression,
    FactSpec,
    MissingFactError,
    compile_expression,
    format_value,
    json_value,
)
from pairly_core.policy_versions import PolicyError

RULES_KIND = "rules"
SCHEMA_ID = "pairly.rules/v1"
MAX_RULES = 200
MAX_FACTS = 200

_RULE_ID = r"^[a-z][a-z0-9_]{0,63}$"
_PLACEHOLDER_RE = re.compile(r"\{([^{}]{0,80})\}")


class Outcome(StrEnum):
    PASS = "pass"  # noqa: S105 (not a password)
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Worst first: the overall outcome of an evaluation is its worst rule outcome.
_OUTCOME_ORDER = (Outcome.ERROR, Outcome.FAIL, Outcome.NEEDS_REVIEW, Outcome.PASS)


def worst_outcome(outcomes: list[Outcome]) -> Outcome:
    for candidate in _OUTCOME_ORDER:
        if candidate in outcomes:
            return candidate
    return Outcome.ERROR  # nothing evaluated: fail closed


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalizedText(_Model):
    en: Annotated[str, Field(min_length=1, max_length=500)]
    ar: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class BilingualText(_Model):
    en: Annotated[str, Field(min_length=1, max_length=500)]
    ar: Annotated[str, Field(min_length=1, max_length=500)]


class FactDecl(_Model):
    type: str
    optional: bool = False

    @field_validator("type")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in FACT_TYPES:
            raise ValueError(f"type must be one of {sorted(FACT_TYPES)}")
        return value


class Rule(_Model):
    id: Annotated[str, Field(pattern=_RULE_ID)]
    description: BilingualText
    when: Annotated[str, Field(min_length=1, max_length=2000)]
    outcome: Literal["pass", "fail", "needs_review"]
    otherwise: Literal["pass", "fail", "needs_review"] = "pass"
    severity: Severity = Severity.MEDIUM
    reason: LocalizedText

    @field_validator("reason", mode="before")
    @classmethod
    def _plain_reason(cls, value: Any) -> Any:
        return {"en": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def _distinct(self) -> Rule:
        if self.outcome == self.otherwise:
            raise ValueError(f"rule {self.id!r}: 'outcome' and 'otherwise' must differ")
        return self


class RuleSet(_Model):
    schema_id: Literal["pairly.rules/v1"] = Field(alias="schema")
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{0,127}$")]
    facts: Annotated[dict[str, FactDecl], Field(min_length=1, max_length=MAX_FACTS)]
    rules: Annotated[list[Rule], Field(min_length=1, max_length=MAX_RULES)]

    _compiled: dict[str, Expression] = PrivateAttr(default_factory=dict)

    @field_validator("facts", mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ({"type": v} if isinstance(v, str) else v) for k, v in value.items()}
        return value

    @field_validator("facts")
    @classmethod
    def _paths(cls, value: dict[str, FactDecl]) -> dict[str, FactDecl]:
        for path in value:
            if not FACT_PATH_RE.fullmatch(path):
                raise ValueError(f"invalid fact name {path!r}")
        paths = set(value)
        for path in paths:
            parts = path.split(".")
            for i in range(1, len(parts)):
                if ".".join(parts[:i]) in paths:
                    raise ValueError(f"fact {path!r} is nested under another fact")
        return value

    @model_validator(mode="after")
    def _compile(self) -> RuleSet:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        specs = self.fact_specs()
        compiled: dict[str, Expression] = {}
        for rule in self.rules:
            try:
                compiled[rule.id] = compile_expression(rule.when, specs)
            except ExprError as exc:
                raise ValueError(f"rule {rule.id!r}: {exc}") from None
            for text in (rule.reason.en, rule.reason.ar or ""):
                for name in _PLACEHOLDER_RE.findall(text):
                    if name not in specs:
                        raise ValueError(
                            f"rule {rule.id!r}: the reason uses {{{name}}}, which is not a fact"
                        )
        self._compiled = compiled
        return self

    def fact_specs(self) -> dict[str, FactSpec]:
        return {k: FactSpec(v.type, v.optional) for k, v in self.facts.items()}

    def expression(self, rule_id: str) -> Expression:
        return self._compiled[rule_id]


def parse_rule_set(content: str) -> RuleSet:
    """Parse and validate a rule set document. Raises ``PolicyError`` with a short reason."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        raise PolicyError("the rule set is not valid YAML") from None
    if not isinstance(data, dict):
        raise PolicyError("the rule set must be a mapping")
    try:
        return RuleSet.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'rules'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)[:10]
        )
        raise PolicyError(f"invalid rule set: {problems}") from None


def rule_set_validator(name: str) -> Any:
    """A ``validate`` callable for ``publish_policy_version``: valid and for this name."""

    def validate(content: str) -> RuleSet:
        rule_set = parse_rule_set(content)
        if rule_set.name != name:
            raise PolicyError("the rule set's name does not match the policy name")
        return rule_set

    return validate


# ------------------------------------------------------------------ evaluation


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_id: str
    outcome: Outcome
    severity: Severity
    reason: str
    reason_ar: str | None
    # The fact values this rule read, JSON-safe (numbers as exact strings).
    facts: dict[str, Any]
    error_code: str | None = None
    triggered: bool | None = None


@dataclass(frozen=True, slots=True)
class RuleSetEvaluation:
    name: str
    as_of: date
    results: tuple[RuleOutcome, ...]
    overall: Outcome = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "overall", worst_outcome([r.outcome for r in self.results]))

    @property
    def requires_human(self) -> bool:
        return self.overall is not Outcome.PASS


def render_reason(template: str, values: Mapping[str, Any]) -> str:
    """Fill ``{fact}`` placeholders by plain substitution (no format spec, no attributes)."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return format_value(values[name]) if name in values else "?"

    return _PLACEHOLDER_RE.sub(repl, template)


def _error(rule: Rule, code: str, message: str, used: dict[str, Any]) -> RuleOutcome:
    return RuleOutcome(
        rule_id=rule.id,
        outcome=Outcome.ERROR,
        severity=rule.severity,
        reason=f"Could not evaluate: {message}. A person must review this.",
        reason_ar="تعذّر تقييم القاعدة. يجب أن يراجعها شخص.",
        facts={k: json_value(v) for k, v in used.items()},
        error_code=code,
    )


def evaluate_rule(
    rule_set: RuleSet, rule: Rule, facts: Mapping[str, Any], as_of: date
) -> RuleOutcome:
    specs = rule_set.fact_specs()
    env = Environment(facts, specs, as_of=as_of)
    expression = rule_set.expression(rule.id)
    try:
        # Required facts are checked up front, so short-circuit logic can never skip
        # over a missing one: fail closed regardless of evaluation order.
        for path in sorted(expression.facts):
            if not specs[path].optional:
                env.get(path)
        triggered = expression.evaluate(env)
    except MissingFactError as exc:
        return _error(rule, exc.code, exc.message, env.used())
    except EvalError as exc:
        return _error(rule, exc.code, exc.message, env.used())
    except Exception:  # any unexpected failure is an error result, never a pass
        return _error(rule, "internal_error", "the rule could not be evaluated", env.used())
    used = env.used()
    outcome = Outcome(rule.outcome if triggered else rule.otherwise)
    if triggered:
        reason = render_reason(rule.reason.en, used)
        reason_ar = render_reason(rule.reason.ar, used) if rule.reason.ar else None
    else:
        reason = f"Condition not met: {rule.description.en}"
        reason_ar = f"الشرط غير متحقق: {rule.description.ar}"
    return RuleOutcome(
        rule_id=rule.id,
        outcome=outcome,
        severity=rule.severity,
        reason=reason,
        reason_ar=reason_ar,
        facts={k: json_value(v) for k, v in used.items()},
        triggered=triggered,
    )


def evaluate_rule_set(
    rule_set: RuleSet, facts: Mapping[str, Any], *, as_of: date
) -> RuleSetEvaluation:
    """Evaluate every rule. Pure: no I/O, deterministic for the same facts and ``as_of``."""
    if not isinstance(facts, Mapping):
        raise PolicyError("facts must be a mapping")
    results = tuple(evaluate_rule(rule_set, rule, facts, as_of) for rule in rule_set.rules)
    return RuleSetEvaluation(name=rule_set.name, as_of=as_of, results=results)

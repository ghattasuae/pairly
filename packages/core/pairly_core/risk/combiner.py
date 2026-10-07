# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Combine risk signals under a versioned risk policy (policy as code).

A risk policy is YAML, validated here and stored in ``policy_versions`` (kind
``risk_policy``, name the assessment type such as ``pay.invoice``)::

    schema: pairly.risk_policy/v1
    name: pay.invoice
    method: noisy_or          # noisy_or (default) or max
    review_threshold: 0.5     # combined score at or above this needs a person
    force_review_severity: high   # any signal this severe or worse needs a person
    default_weight: 1.0
    weights:                  # per detector, 0..1
      core.amount_outlier: 0.6
    required_detectors: [core.guardrail_findings, core.extraction_confidence]
    levels: {medium: 0.3, high: 0.6, critical: 0.9}

Combined score: ``noisy_or`` is ``1 - prod(1 - weight * score)``, so one strong signal
is enough and several weaker ones add up; ``max`` is ``max(weight * score)``.

Hard rules, enforced in code whatever the policy says (fail closed):

* any ``critical`` signal forces human review;
* any ``error`` signal (a check that could not run) forces human review and makes the
  level at least ``high``;
* a required detector with no signal forces human review;
* no policy at all forces human review.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pairly_core.policy_versions import PolicyError, PublishedPolicy
from pairly_core.risk.types import RiskSignal, Severity, SignalStatus

POLICY_KIND = "risk_policy"
SCHEMA_ID = "pairly.risk_policy/v1"

DetectorName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}$")]
Weight = Annotated[float, Field(ge=0.0, le=1.0)]


class Levels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    medium: Weight = 0.3
    high: Weight = 0.6
    critical: Weight = 0.9

    @model_validator(mode="after")
    def _ascending(self) -> Levels:
        if not self.medium <= self.high <= self.critical:
            raise ValueError("levels must be ascending: medium <= high <= critical")
        return self

    def level(self, score: float) -> Severity:
        if score >= self.critical:
            return Severity.CRITICAL
        if score >= self.high:
            return Severity.HIGH
        if score >= self.medium:
            return Severity.MEDIUM
        return Severity.LOW


class RiskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["pairly.risk_policy/v1"] = Field(alias="schema")
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_.]{0,63}$")]
    method: Literal["noisy_or", "max"] = "noisy_or"
    review_threshold: Weight = 0.5
    force_review_severity: Literal["low", "medium", "high", "critical"] = "high"
    default_weight: Weight = 1.0
    weights: dict[DetectorName, Weight] = Field(default_factory=dict, max_length=200)
    required_detectors: list[DetectorName] = Field(default_factory=list, max_length=200)
    levels: Levels = Field(default_factory=Levels)

    def weight(self, detector: str) -> float:
        return self.weights.get(detector, self.default_weight)


def parse_risk_policy(content: str) -> RiskPolicy:
    """Parse and validate a risk policy. Raises ``PolicyError`` with a short reason."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        raise PolicyError("the risk policy is not valid YAML") from None
    if not isinstance(data, dict):
        raise PolicyError("the risk policy must be a mapping")
    try:
        return RiskPolicy.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'policy'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)[:10]
        )
        raise PolicyError(f"invalid risk policy: {problems}") from None


def risk_policy_validator(name: str) -> Callable[[str], RiskPolicy]:
    """A ``validate`` callable for ``publish_policy_version``: valid and for ``name``."""

    def validate(content: str) -> RiskPolicy:
        policy = parse_risk_policy(content)
        if policy.name != name:
            raise PolicyError("the risk policy is for another assessment type")
        return policy

    return validate


@dataclass(frozen=True, slots=True)
class WeightedSignal:
    signal: RiskSignal
    weight: float


@dataclass(frozen=True, slots=True)
class CombinedRisk:
    score: float
    level: Severity
    requires_human: bool
    reasons: tuple[str, ...]
    signals: tuple[WeightedSignal, ...]
    policy_version_id: uuid.UUID | None
    policy_version: int | None
    policy_hash: str | None


def combine(
    signals: Sequence[RiskSignal],
    policy: RiskPolicy | None,
    published: PublishedPolicy | None = None,
) -> CombinedRisk:
    """Combine ``signals`` under ``policy``. Pure and deterministic."""
    reasons: list[str] = []
    if policy is None:
        weighted = tuple(WeightedSignal(s, 1.0) for s in signals)
        reasons.append("risk_policy_missing")
        threshold, method = 1.0, "max"
        force_rank = Severity.HIGH.rank
        required: list[str] = []
        levels = Levels()
    else:
        weighted = tuple(WeightedSignal(s, policy.weight(s.detector)) for s in signals)
        threshold, method = policy.review_threshold, policy.method
        force_rank = Severity(policy.force_review_severity).rank
        required = policy.required_detectors
        levels = policy.levels

    contributions = [w.weight * w.signal.score for w in weighted]
    if method == "max":
        score = max(contributions, default=0.0)
    else:
        remaining = 1.0
        for c in contributions:
            remaining *= 1.0 - c
        score = 1.0 - remaining
    score = round(min(max(score, 0.0), 1.0), 6)

    level = levels.level(score)
    seen = {s.detector for s in signals}
    for name in required:
        if name not in seen:
            reasons.append(f"required_detector_missing:{name}")
    for s in signals:
        if s.status is SignalStatus.ERROR:
            reasons.append(f"detector_error:{s.detector}")
            if level.rank < Severity.HIGH.rank:
                level = Severity.HIGH
        elif s.severity is Severity.CRITICAL:
            reasons.append(f"critical_signal:{s.detector}")
            level = Severity.CRITICAL
        elif s.severity.rank >= force_rank:
            reasons.append(f"severe_signal:{s.detector}")
            if level.rank < s.severity.rank:
                level = s.severity
    if policy is not None and score >= threshold:
        reasons.append("score_at_or_above_threshold")
    return CombinedRisk(
        score=score,
        level=level,
        requires_human=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        signals=weighted,
        policy_version_id=published.id if published is not None else None,
        policy_version=published.version if published is not None else None,
        policy_hash=published.content_hash if published is not None else None,
    )

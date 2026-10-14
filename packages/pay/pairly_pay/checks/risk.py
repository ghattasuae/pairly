# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The invoice risk score (brief 4.4.9): every check as a risk signal, combined by the
engine (``pairly_core.risk``) under the ``pay.invoice`` risk policy
(``policy/invoice_risk_policy.yaml``, published as version 1 for an organisation the
first time it is needed).

Each check is one detector (``pay.<check>``) in ``PAY_DETECTORS``. It reads the check's
result from ``RiskContext.facts["checks"]`` and turns it into a signal:

* ``error`` result: an error signal (high risk, the combiner always asks a person);
* findings: score ``1 - prod(1 - s)`` over the findings (info 0, low 0.2, medium 0.45,
  high 0.85, critical 1.0), severity the worst finding's;
* no finding: score 0.

The combiner's hard rules apply on top (a critical or error signal, or a missing
detector, always needs a person). High-risk invoices therefore always go to a person,
whatever their amount.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Final

from pairly_core.risk.combiner import CombinedRisk, RiskPolicy, combine, parse_risk_policy
from pairly_core.risk.registry import DetectorRegistry, run_detectors
from pairly_core.risk.types import (
    DetectorTools,
    Evidence,
    RiskContext,
    RiskSignal,
    Severity,
)
from pairly_pay.checks.messages import explain, term
from pairly_pay.checks.types import CheckResult, CheckStatus

ASSESSMENT_TYPE: Final = "pay.invoice"
CHECK_NAMES: Final = (
    "arithmetic",
    "duplicates",
    "bank",
    "impersonation",
    "amounts",
    "forgery",
    "injection",
    "matching",
    "policy",
)
RISK_POLICY_V1: Final = (
    files("pairly_pay").joinpath("policy", "invoice_risk_policy.yaml").read_text(encoding="utf-8")
)
SCORE: Final = {
    Severity.INFO: 0.0,
    Severity.LOW: 0.2,
    Severity.MEDIUM: 0.45,
    Severity.HIGH: 0.85,
    Severity.CRITICAL: 1.0,
}


def signal_of(name: str, version: str, outcome: CheckResult | None) -> RiskSignal:
    detector = f"pay.{name}"
    if outcome is None:
        return RiskSignal.error(detector, version, "check_missing")
    if outcome.status is CheckStatus.ERROR:
        return RiskSignal.error(detector, version, outcome.error or "check_error")
    if not outcome.findings:
        return RiskSignal(
            detector=detector,
            version=version,
            score=0.0,
            label="clean",
            severity=Severity.INFO,
            explanation_key=f"pay.{name}.clean",
        )
    remaining = 1.0
    for f in outcome.findings:
        remaining *= 1.0 - SCORE[f.severity]
    worst = outcome.worst or Severity.INFO
    return RiskSignal(
        detector=detector,
        version=version,
        score=round(min(max(1.0 - remaining, 0.0), 1.0), 6),
        label="findings",
        severity=worst,
        evidence=[
            Evidence(
                kind="pay_finding",
                detail={"code": f.code, "rule": f.rule, "severity": f.severity.value},
            )
            for f in outcome.findings[:50]
        ],
        explanation_key=f"pay.{name}.findings",
    )


@dataclass(frozen=True, slots=True)
class CheckDetector:
    """A Pay check as an engine risk detector (pure: reads the context only)."""

    check: str
    version: str = "1"
    needs_model: bool = False

    @property
    def name(self) -> str:
        return f"pay.{self.check}"

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        checks = ctx.facts.get("checks")
        outcome = checks.get(self.check) if isinstance(checks, Mapping) else None
        return signal_of(self.check, self.version, outcome)


PAY_DETECTORS: Final = DetectorRegistry(CheckDetector(name) for name in CHECK_NAMES)


def default_policy() -> RiskPolicy:
    return parse_risk_policy(RISK_POLICY_V1)


async def score(
    checks: Mapping[str, CheckResult], ctx_base: RiskContext | None = None
) -> CombinedRisk:
    """Combine without storing (the eval, and a preview): the bundled policy."""
    ctx = ctx_base or RiskContext(
        org_id=uuid.UUID(int=0),
        workflow="pay",
        entity_type="pay.invoice",
        entity_id="preview",
        facts={"checks": dict(checks)},
    )
    signals = await run_detectors(PAY_DETECTORS.select(), ctx, DetectorTools())
    return combine(signals, default_policy())


def explanation(combined: CombinedRisk) -> dict[str, str]:
    """One sentence on the risk, in English and Arabic."""
    active = [w for w in combined.signals if w.weight * w.signal.score > 0]
    if not active and not combined.requires_human:
        en, ar = explain("risk_clean", {})
        return {"en": en, "ar": ar}
    strongest = max(combined.signals, key=lambda w: w.weight * w.signal.score, default=None)
    check = strongest.signal.detector.split(".", 1)[1] if strongest is not None else "policy"
    en, ar = explain(
        "risk_summary",
        {"level": term(combined.level.value), "count": len(active), "check": term(check)},
    )
    return {"en": en, "ar": ar}


def as_json(combined: CombinedRisk, assessment_id: str | None) -> dict[str, Any]:
    return {
        "assessment_id": assessment_id,
        "score": combined.score,
        "level": combined.level.value,
        "requires_human": combined.requires_human,
        "reasons": list(combined.reasons[:20]),
        "policy_version": combined.policy_version,
        "explanation": explanation(combined),
        "signals": [
            {
                "detector": w.signal.detector,
                "status": w.signal.status.value,
                "score": w.signal.score,
                "weight": w.weight,
                "severity": w.signal.severity.value,
            }
            for w in combined.signals
        ],
    }

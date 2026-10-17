# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Generic detectors every workflow can use.

* ``core.guardrail_findings``     guardrail findings on the document (injection, hidden
  text, ...). A missing report, a report of another text, or a guardrail detector that
  failed is an ``error`` signal: the check did not (fully) run.
* ``core.extraction_confidence``  required fields missing or flagged, other fields
  flagged; an extraction that failed (model down) is an ``error`` signal.
* ``core.duplicate_hash``         the same file was already seen on another entity.
* ``core.amount_outlier``         the amount is unusual against history (median/MAD).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pairly_core.guardrails.report import Severity as GuardrailSeverity
from pairly_core.guardrails.report import text_digest
from pairly_core.risk.stats import DEFAULT_MIN_HISTORY, DEFAULT_THRESHOLD, amount_outlier
from pairly_core.risk.types import (
    DetectorTools,
    Evidence,
    RiskContext,
    RiskSignal,
    Severity,
)

_SEVERITY_SCORE = {
    Severity.INFO: 0.05,
    Severity.LOW: 0.25,
    Severity.MEDIUM: 0.5,
    Severity.HIGH: 0.85,
    Severity.CRITICAL: 1.0,
}
MAX_EVIDENCE = 50


# Guardrail severity to risk severity. A high finding is a manipulation attempt (hidden
# text, a direct instruction to the model): critical, which the combiner always sends to
# a person whatever the policy says. A medium finding is "flagged for a person" in the
# guardrails' own terms, so it maps to high (the default policy forces review on high).
_GUARDRAIL_SEVERITY = {
    GuardrailSeverity.LOW: Severity.LOW,
    GuardrailSeverity.MEDIUM: Severity.HIGH,
    GuardrailSeverity.HIGH: Severity.CRITICAL,
}


def _severity(value: GuardrailSeverity) -> Severity:
    """Risk severity of a guardrail finding; anything unknown counts as critical."""
    return _GUARDRAIL_SEVERITY.get(value, Severity.CRITICAL)


def _clean_signal(name: str, version: str, label: str, key: str) -> RiskSignal:
    return RiskSignal(
        detector=name,
        version=version,
        score=0.0,
        label=label,
        severity=Severity.INFO,
        explanation_key=key,
    )


@dataclass(frozen=True, slots=True)
class GuardrailFindingsDetector:
    name: str = "core.guardrail_findings"
    version: str = "1"
    needs_model: bool = False

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        report = ctx.guardrail_report
        if report is None:
            return RiskSignal.error(self.name, self.version, "guardrail_report_missing")
        if ctx.document is not None and (
            report.text_sha256 != text_digest(ctx.document.text)
            or report.text_length != len(ctx.document.text)
        ):
            return RiskSignal.error(self.name, self.version, "guardrail_report_mismatch")
        if report.errors:
            return RiskSignal.error(self.name, self.version, "guardrail_detector_failed")
        findings = list(report.findings)
        if not findings:
            return _clean_signal(self.name, self.version, "clean", "risk.guardrails.clean")
        severities = [_severity(f.severity) for f in findings]
        worst = max(severities, key=lambda s: s.rank)
        return RiskSignal(
            detector=self.name,
            version=self.version,
            score=_SEVERITY_SCORE[worst],
            label="guardrail_findings",
            severity=worst,
            evidence=[
                Evidence(
                    kind="guardrail_finding",
                    detail={
                        "finding": f.id[:16],
                        "type": f.type.value,
                        "code": f.code[:64],
                        "guardrail_severity": f.severity.value,
                        "severity": s.value,
                        "location": f.location[:64],
                        "page": f.page,
                    },
                )
                for f, s in list(zip(findings, severities, strict=True))[:MAX_EVIDENCE]
            ],
            explanation_key="risk.guardrails.findings",
        )


@dataclass(frozen=True, slots=True)
class ExtractionConfidenceDetector:
    name: str = "core.extraction_confidence"
    version: str = "1"
    needs_model: bool = False

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        result = ctx.extraction
        if result is None:
            return RiskSignal.error(self.name, self.version, "extraction_missing")
        if result.data is None:
            reason = result.reasons[0] if result.reasons else "extraction_failed"
            return RiskSignal.error(self.name, self.version, reason.split(":", 1)[0])
        flagged = [f for f in result.fields if f.flags]
        required = [f for f in flagged if f.required]
        # Lists with no items have no field row; their reason names them.
        known = {f.path for f in result.fields}
        list_missing = [
            r
            for r in result.reasons
            if r.startswith("missing:") and r.split(":", 1)[1] not in known
        ]
        if not flagged and not list_missing:
            return _clean_signal(self.name, self.version, "confident", "risk.extraction.ok")
        if required or list_missing:
            severity, score = Severity.HIGH, 0.85
        else:
            lowest = min(f.confidence for f in flagged)
            severity, score = Severity.MEDIUM, max(0.3, 0.6 - lowest / 2)
        evidence = [
            Evidence(
                kind="extraction_field",
                detail={
                    "path": f.path[:128],
                    "flag": f.flags[0].value,
                    "required": f.required,
                },
                start=f.spans[0].start if f.spans else None,
                end=f.spans[0].end if f.spans else None,
            )
            for f in flagged[:MAX_EVIDENCE]
        ]
        evidence += [
            Evidence(
                kind="extraction_field", detail={"path": r.split(":", 1)[1], "flag": "missing"}
            )
            for r in list_missing[: max(0, MAX_EVIDENCE - len(evidence))]
        ]
        return RiskSignal(
            detector=self.name,
            version=self.version,
            score=score,
            label="low_confidence_extraction",
            severity=severity,
            evidence=evidence,
            explanation_key="risk.extraction.low_confidence",
        )


@dataclass(frozen=True, slots=True)
class DuplicateHashDetector:
    name: str = "core.duplicate_hash"
    version: str = "1"
    needs_model: bool = False

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        if ctx.document_sha256 is None:
            return RiskSignal.error(self.name, self.version, "document_hash_missing")
        others = [
            m
            for m in ctx.hash_matches
            if (m.entity_type, m.entity_id) != (ctx.entity_type, ctx.entity_id)
        ]
        if not others:
            return _clean_signal(self.name, self.version, "unique_file", "risk.duplicate.none")
        return RiskSignal(
            detector=self.name,
            version=self.version,
            score=0.9,
            label="duplicate_file",
            severity=Severity.HIGH,
            evidence=[
                Evidence(
                    kind="same_file_hash",
                    detail={
                        "entity_type": m.entity_type[:64],
                        "entity_id": m.entity_id[:128],
                        "workflow": m.workflow,
                    },
                )
                for m in others[:MAX_EVIDENCE]
            ],
            explanation_key="risk.duplicate.same_file",
        )


@dataclass(frozen=True, slots=True)
class AmountOutlierDetector:
    name: str = "core.amount_outlier"
    version: str = "1"
    needs_model: bool = False
    threshold: Decimal = DEFAULT_THRESHOLD
    min_history: int = DEFAULT_MIN_HISTORY

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        if ctx.amount is None:
            return RiskSignal.error(self.name, self.version, "amount_missing")
        result = amount_outlier(
            ctx.amount, ctx.amount_history, threshold=self.threshold, min_history=self.min_history
        )
        detail: dict[str, str | int | bool | None] = {
            "history_size": result.history_size,
            "median": None if result.median is None else str(result.median),
            "z_score": None if result.z_score is None else str(_round(result.z_score)),
        }
        if not result.enough_history:
            return RiskSignal(
                detector=self.name,
                version=self.version,
                score=0.0,
                label="insufficient_history",
                severity=Severity.INFO,
                evidence=[Evidence(kind="amount_history", detail=detail)],
                explanation_key="risk.amount.insufficient_history",
            )
        if result.z_score is None:  # pragma: no cover - set whenever there is history
            return RiskSignal.error(self.name, self.version, "no_score")
        z = abs(result.z_score)
        if not result.is_outlier:
            score = float(min(z / self.threshold, Decimal(1))) * 0.4
            return RiskSignal(
                detector=self.name,
                version=self.version,
                score=round(score, 4),
                label="usual_amount",
                severity=Severity.INFO,
                evidence=[Evidence(kind="amount_history", detail=detail)],
                explanation_key="risk.amount.usual",
            )
        extreme = z > 2 * self.threshold
        return RiskSignal(
            detector=self.name,
            version=self.version,
            score=0.9 if extreme else 0.6,
            label="amount_outlier",
            severity=Severity.HIGH if extreme else Severity.MEDIUM,
            evidence=[Evidence(kind="amount_history", detail=detail)],
            explanation_key="risk.amount.outlier",
        )


def _round(value: Decimal) -> Decimal:
    return value if not value.is_finite() else value.quantize(Decimal("0.01"))


def generic_detectors() -> tuple[
    GuardrailFindingsDetector,
    ExtractionConfidenceDetector,
    DuplicateHashDetector,
    AmountOutlierDetector,
]:
    return (
        GuardrailFindingsDetector(),
        ExtractionConfidenceDetector(),
        DuplicateHashDetector(),
        AmountOutlierDetector(),
    )

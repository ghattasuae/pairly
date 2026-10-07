# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Risk detectors, robust statistics, the fail-closed runner and the combiner."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pairly_core.documents.model import NormalizedDocument, Page, ParserInfo
from pairly_core.extraction.engine import (
    ExtractionResult,
    ExtractionStatus,
    FieldResult,
    ModelCallInfo,
)
from pairly_core.extraction.types import FieldFlag
from pairly_core.guardrails import Finding as GFinding
from pairly_core.guardrails import FindingType, GuardrailReport, Span
from pairly_core.guardrails import Severity as GSeverity
from pairly_core.guardrails.report import text_digest
from pairly_core.llm.errors import BudgetExceeded
from pairly_core.policy_versions import PolicyError, PublishedPolicy
from pairly_core.risk.combiner import combine, parse_risk_policy, risk_policy_validator
from pairly_core.risk.detectors import (
    AmountOutlierDetector,
    DuplicateHashDetector,
    ExtractionConfidenceDetector,
    GuardrailFindingsDetector,
    generic_detectors,
)
from pairly_core.risk.registry import DetectorError, DetectorRegistry, run_detector, run_detectors
from pairly_core.risk.stats import amount_outlier, mad, median
from pairly_core.risk.types import (
    DetectorTools,
    EntityRef,
    RiskContext,
    RiskSignal,
    Severity,
    SignalStatus,
)

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
TOOLS = DetectorTools()


def ctx(**kw: Any) -> RiskContext:
    return RiskContext(org_id=ORG, workflow="pay", entity_type="invoice", entity_id="inv-1", **kw)


def sig(name: str, severity: Severity, score: float, *, error: bool = False) -> RiskSignal:
    if error:
        return RiskSignal.error(name, "1", "boom")
    return RiskSignal(
        detector=name,
        version="1",
        score=score,
        label="x",
        severity=severity,
        explanation_key="risk.test",
    )


POLICY = """\
schema: pairly.risk_policy/v1
name: pay.invoice
method: noisy_or
review_threshold: 0.5
force_review_severity: high
default_weight: 1.0
weights:
  core.amount_outlier: 0.5
required_detectors: [core.guardrail_findings]
levels: {medium: 0.3, high: 0.6, critical: 0.9}
"""


def published(content: str = POLICY) -> PublishedPolicy:
    return PublishedPolicy(
        id=uuid.uuid4(),
        kind="risk_policy",
        name="pay.invoice",
        version=3,
        content=content,
        content_hash="a" * 64,
        created_at=datetime.now(UTC),
    )


# ------------------------------------------------------------------ stats


def test_median_mad_and_outlier() -> None:
    history = [Decimal(v) for v in ("100", "102", "98", "101", "99", "250")]
    assert median(history) == Decimal("100.5")
    assert mad(history) == Decimal("1.5")
    usual = amount_outlier(Decimal("103"), history)
    assert usual.enough_history
    assert not usual.is_outlier
    # The 250 in history does not hide a new 5000 (robust, unlike mean and stdev).
    big = amount_outlier(Decimal("5000"), history)
    assert big.is_outlier
    assert big.z_score is not None
    assert big.z_score > 1000
    short = amount_outlier(Decimal("5000"), history[:3])
    assert not short.enough_history
    assert short.z_score is None


def test_outlier_constant_history() -> None:
    flat = [Decimal("100")] * 6
    assert amount_outlier(Decimal("100"), flat).is_outlier is False
    result = amount_outlier(Decimal("101"), flat)
    assert result.is_outlier
    assert result.z_score == Decimal("Infinity")
    # More than half equal: MAD 0, the mean absolute deviation takes over.
    mostly = [Decimal("100")] * 5 + [Decimal("110"), Decimal("90")]
    assert amount_outlier(Decimal("104"), mostly).is_outlier is False
    with pytest.raises(ValueError, match="threshold"):
        amount_outlier(Decimal(1), flat, threshold=Decimal(0))


# ------------------------------------------------------------------ detectors


def make_doc(lines: list[str]) -> NormalizedDocument:
    return NormalizedDocument(
        media_type="text/plain",
        parser=ParserInfo(name="synthetic", version="1"),
        pages=[Page(number=1, width=612.0, height=792.0, unit="pt", text="\n".join(lines))],
    )


def _gfinding(kind: FindingType, code: str, severity: GSeverity) -> GFinding:
    return GFinding(
        id="",
        type=kind,
        code=code,
        severity=severity,
        detector="test",
        evidence="x",
        spans=(Span(start=0, end=1),),
    )


def _greport(doc: NormalizedDocument, *findings: GFinding, errors: tuple[str, ...] = ()) -> Any:
    return GuardrailReport(
        findings=tuple(f.model_copy(update={"id": f"g{i + 1}"}) for i, f in enumerate(findings)),
        errors=errors,
        text_sha256=text_digest(doc.text),
        text_length=len(doc.text),
    )


async def test_guardrail_detector() -> None:
    detector = GuardrailFindingsDetector()
    doc = make_doc(["Invoice No: INV-1", "Total: AED 10.00"])
    other = make_doc(["Something else"])
    missing = await detector.detect(ctx(document=doc), TOOLS)
    assert missing.status is SignalStatus.ERROR
    clean = await detector.detect(ctx(document=doc, guardrail_report=_greport(doc)), TOOLS)
    assert (clean.score, clean.severity) == (0.0, Severity.INFO)
    found = await detector.detect(
        ctx(
            document=doc,
            guardrail_report=_greport(
                doc,
                _gfinding(FindingType.UNUSUAL_UNICODE, "zero_width", GSeverity.LOW),
                _gfinding(FindingType.HIDDEN_TEXT, "white_on_white", GSeverity.HIGH),
            ),
        ),
        TOOLS,
    )
    # A high guardrail finding is a manipulation attempt: critical, always a person.
    assert found.severity is Severity.CRITICAL
    assert found.score == 1.0
    assert [(e.detail["type"], e.detail["code"]) for e in found.evidence] == [
        ("unusual_unicode", "zero_width"),
        ("hidden_text", "white_on_white"),
    ]
    assert [e.detail["finding"] for e in found.evidence] == ["g1", "g2"]
    medium = await detector.detect(
        ctx(
            document=doc,
            guardrail_report=_greport(
                doc, _gfinding(FindingType.KEYWORD_STUFFING, "repeated_terms", GSeverity.MEDIUM)
            ),
        ),
        TOOLS,
    )
    assert medium.severity is Severity.HIGH
    low = await detector.detect(
        ctx(
            document=doc,
            guardrail_report=_greport(
                doc, _gfinding(FindingType.UNUSUAL_UNICODE, "zero_width", GSeverity.LOW)
            ),
        ),
        TOOLS,
    )
    assert low.severity is Severity.LOW
    # A detector that failed, or a report of another document, is an error (fail closed).
    failed = await detector.detect(
        ctx(document=doc, guardrail_report=_greport(doc, errors=("hidden_text: KeyError",))),
        TOOLS,
    )
    assert failed.status is SignalStatus.ERROR
    mismatch = await detector.detect(ctx(document=doc, guardrail_report=_greport(other)), TOOLS)
    assert mismatch.status is SignalStatus.ERROR


def _field(path: str, flags: tuple[FieldFlag, ...], required: bool, conf: float) -> FieldResult:
    return FieldResult(
        path=path,
        pattern=path,
        kind="text",
        value="v" if conf else None,
        raw=None,
        confidence=conf,
        threshold=0.7,
        required=required,
        flags=flags,
        spans=(),
    )


def _extraction(*fields: FieldResult, data: bool = True, reasons: tuple[str, ...] = ()) -> Any:
    return ExtractionResult(
        schema=None,  # type: ignore[arg-type]
        status=ExtractionStatus.NEEDS_HUMAN if reasons else ExtractionStatus.OK,
        reasons=reasons,
        fields=fields,
        data=object() if data else None,  # type: ignore[arg-type]
        model_call=ModelCallInfo((), None, "core_extract", "v1", None, None, None, False),
    )


async def test_extraction_confidence_detector() -> None:
    detector = ExtractionConfidenceDetector()
    assert (await detector.detect(ctx(), TOOLS)).status is SignalStatus.ERROR
    failed = await detector.detect(
        ctx(extraction=_extraction(data=False, reasons=("model_unavailable:x",))), TOOLS
    )
    assert failed.status is SignalStatus.ERROR
    assert failed.evidence[0].detail["reason"] == "model_unavailable"
    ok = await detector.detect(ctx(extraction=_extraction(_field("a", (), True, 0.9))), TOOLS)
    assert ok.score == 0.0
    optional = await detector.detect(
        ctx(extraction=_extraction(_field("b", (FieldFlag.LOW_CONFIDENCE,), False, 0.5))), TOOLS
    )
    assert optional.severity is Severity.MEDIUM
    required = await detector.detect(
        ctx(extraction=_extraction(_field("c", (FieldFlag.NO_EVIDENCE,), True, 0.0))), TOOLS
    )
    assert required.severity is Severity.HIGH
    assert required.evidence[0].detail == {"path": "c", "flag": "no_evidence", "required": True}
    no_items = await detector.detect(
        ctx(extraction=_extraction(reasons=("missing:line_items",))), TOOLS
    )
    assert no_items.severity is Severity.HIGH


async def test_duplicate_hash_detector() -> None:
    detector = DuplicateHashDetector()
    assert (await detector.detect(ctx(), TOOLS)).status is SignalStatus.ERROR
    sha = "b" * 64
    self_only = ctx(document_sha256=sha, hash_matches=(EntityRef("invoice", "inv-1"),))
    assert (await detector.detect(self_only, TOOLS)).label == "unique_file"
    dup = await detector.detect(
        ctx(document_sha256=sha, hash_matches=(EntityRef("invoice", "inv-0", "pay"),)), TOOLS
    )
    assert (dup.label, dup.severity) == ("duplicate_file", Severity.HIGH)
    assert dup.evidence[0].detail["entity_id"] == "inv-0"


async def test_amount_outlier_detector() -> None:
    detector = AmountOutlierDetector()
    history = [Decimal(v) for v in ("1000", "1020", "980", "1010", "990")]
    assert (await detector.detect(ctx(), TOOLS)).status is SignalStatus.ERROR
    usual = await detector.detect(ctx(amount=Decimal("1005"), amount_history=history), TOOLS)
    assert usual.label == "usual_amount"
    assert usual.score < 0.4
    few = await detector.detect(ctx(amount=Decimal("1005"), amount_history=history[:2]), TOOLS)
    assert few.label == "insufficient_history"
    huge = await detector.detect(ctx(amount=Decimal("90000"), amount_history=history), TOOLS)
    assert (huge.label, huge.severity) == ("amount_outlier", Severity.HIGH)
    assert huge.evidence[0].detail["median"] == "1000"


# ------------------------------------------------------------------ runner


@dataclass(frozen=True)
class Raising:
    exc: BaseException
    name: str = "core.raising"
    version: str = "1"
    needs_model: bool = False

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        raise self.exc


@dataclass(frozen=True)
class Lying:
    name: str = "core.lying"
    version: str = "1"
    needs_model: bool = False

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        return sig("core.other", Severity.INFO, 0.0)


@dataclass(frozen=True)
class NeedsModel:
    name: str = "core.needs_model"
    version: str = "1"
    needs_model: bool = True

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal:
        return sig(self.name, Severity.INFO, 0.0)


async def test_runner_fails_closed() -> None:
    model_down = await run_detector(Raising(BudgetExceeded("no budget")), ctx(), TOOLS)
    assert model_down.status is SignalStatus.ERROR
    assert model_down.evidence[0].detail["reason"] == "model_unavailable:model_budget_exceeded"
    crashed = await run_detector(Raising(RuntimeError("bug")), ctx(), TOOLS)
    assert crashed.status is SignalStatus.ERROR
    assert crashed.score == 1.0
    assert crashed.severity is Severity.HIGH
    assert (await run_detector(Lying(), ctx(), TOOLS)).status is SignalStatus.ERROR
    assert (await run_detector(NeedsModel(), ctx(), TOOLS)).status is SignalStatus.ERROR
    signals = await run_detectors(list(generic_detectors()), ctx(), TOOLS)
    assert [s.detector for s in signals] == [d.name for d in generic_detectors()]


def test_registry() -> None:
    registry = DetectorRegistry(generic_detectors())
    assert "core.amount_outlier" in registry.names
    with pytest.raises(DetectorError, match="duplicate"):
        registry.register(AmountOutlierDetector())
    with pytest.raises(DetectorError, match="invalid"):
        registry.register(Raising(RuntimeError(), name="Bad Name"))
    with pytest.raises(DetectorError, match="unknown"):
        registry.select(["core.nope"])
    assert [d.name for d in registry.select(["core.duplicate_hash"])] == ["core.duplicate_hash"]
    assert registry.get("core.nope") is None


# ------------------------------------------------------------------ combiner


def test_policy_parsing_and_validation() -> None:
    policy = parse_risk_policy(POLICY)
    assert policy.weight("core.amount_outlier") == 0.5
    assert policy.weight("core.other") == 1.0
    for bad in (
        "a: [",
        "- 1",
        POLICY.replace("0.5\nforce", "1.5\nforce"),
        POLICY.replace("{medium: 0.3, high: 0.6", "{medium: 0.7, high: 0.6"),
        POLICY.replace("method: noisy_or", "method: average"),
        POLICY + "surprise: 1\n",
    ):
        with pytest.raises(PolicyError):
            parse_risk_policy(bad)
    validate = risk_policy_validator("hire.candidate")
    with pytest.raises(PolicyError, match="another"):
        validate(POLICY)


def test_combiner_weights_threshold_and_version() -> None:
    policy = parse_risk_policy(POLICY)
    pub = published()
    low = combine(
        [sig("core.guardrail_findings", Severity.INFO, 0.0), sig("core.a", Severity.LOW, 0.2)],
        policy,
        pub,
    )
    assert low.requires_human is False
    assert low.score == pytest.approx(0.2)
    assert (low.policy_version_id, low.policy_version, low.policy_hash) == (pub.id, 3, "a" * 64)
    # Two medium signals add up past the threshold (noisy-or).
    two = combine(
        [
            sig("core.guardrail_findings", Severity.INFO, 0.0),
            sig("core.a", Severity.MEDIUM, 0.35),
            sig("core.b", Severity.MEDIUM, 0.35),
        ],
        policy,
        pub,
    )
    assert two.score == pytest.approx(1 - 0.65 * 0.65)
    assert two.requires_human
    assert two.reasons == ("score_at_or_above_threshold",)
    # Weights apply.
    weighted = combine(
        [
            sig("core.guardrail_findings", Severity.INFO, 0.0),
            sig("core.amount_outlier", Severity.MEDIUM, 0.6),
        ],
        policy,
        pub,
    )
    assert weighted.score == pytest.approx(0.3)
    assert not weighted.requires_human
    as_max = combine(
        [sig("core.guardrail_findings", Severity.INFO, 0.0), sig("core.a", Severity.MEDIUM, 0.4)],
        parse_risk_policy(POLICY.replace("noisy_or", "max")),
        pub,
    )
    assert as_max.score == pytest.approx(0.4)


def test_hard_rules_override_the_score() -> None:
    policy = parse_risk_policy(POLICY)
    critical = combine(
        [
            sig("core.guardrail_findings", Severity.INFO, 0.0),
            sig("core.c", Severity.CRITICAL, 0.05),
        ],
        policy,
        published(),
    )
    assert critical.score < 0.5
    assert critical.requires_human
    assert critical.level is Severity.CRITICAL
    assert critical.reasons == ("critical_signal:core.c",)
    # A policy cannot switch the critical rule off.
    lenient = parse_risk_policy(
        POLICY.replace("force_review_severity: high", "force_review_severity: critical").replace(
            "review_threshold: 0.5", "review_threshold: 1.0"
        )
    )
    assert combine(
        [sig("core.guardrail_findings", Severity.INFO, 0.0), sig("core.c", Severity.CRITICAL, 0.0)],
        lenient,
    ).requires_human
    errored = combine(
        [
            sig("core.guardrail_findings", Severity.INFO, 0.0),
            sig("core.e", Severity.INFO, 0, error=True),
        ],
        lenient,
    )
    assert errored.requires_human
    assert errored.level.rank >= Severity.HIGH.rank
    assert "detector_error:core.e" in errored.reasons
    severe = combine(
        [sig("core.guardrail_findings", Severity.INFO, 0.0), sig("core.h", Severity.HIGH, 0.1)],
        policy,
    )
    assert severe.reasons == ("severe_signal:core.h",)
    missing = combine([sig("core.a", Severity.INFO, 0.0)], policy)
    assert missing.reasons == ("required_detector_missing:core.guardrail_findings",)
    no_policy = combine([sig("core.a", Severity.INFO, 0.0)], None)
    assert no_policy.requires_human
    assert no_policy.policy_version_id is None
    assert no_policy.reasons == ("risk_policy_missing",)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Risk signals, the detector contract and the typed context detectors read.

A detector is a pure function of a ``RiskContext``: everything it needs (the document,
the extraction, the guardrail report, history looked up beforehand) is in the context,
so a detector never reads the database and the same context always gives the same
signal. A detector may declare ``needs_model``; it then gets the model router through
``DetectorTools``, and a ``ModelUnavailable`` becomes an ``error`` signal (high risk,
fail closed). Any other exception a detector raises also becomes an ``error`` signal.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from pairly_core.documents.model import NormalizedDocument
from pairly_core.guardrails.report import GuardrailReport

if TYPE_CHECKING:
    from pairly_core.extraction.engine import ExtractionResult
    from pairly_core.llm.router import ModelRouter

_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}$"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {s: i for i, s in enumerate(Severity)}


class SignalStatus(StrEnum):
    OK = "ok"
    # The check could not run. Counts as high risk and always needs a person.
    ERROR = "error"


class Evidence(BaseModel):
    """One piece of evidence: what was seen, with ids and offsets, never free PII."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    detail: dict[str, str | int | bool | None] = Field(default_factory=dict)
    # Character offsets into the original document text, when the evidence has a place.
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class RiskSignal(BaseModel):
    """What one detector concluded. ``score`` 0 (no risk) to 1 (certain risk)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detector: str = Field(pattern=_NAME_PATTERN)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
    status: SignalStatus = SignalStatus.OK
    score: float = Field(ge=0.0, le=1.0)
    label: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    severity: Severity
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    # A message key the UI translates (en, ar), never a free-text model explanation.
    explanation_key: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,127}$")

    @classmethod
    def error(cls, detector: str, version: str, reason: str) -> RiskSignal:
        """The check could not run: high risk, fail closed."""
        return cls(
            detector=detector,
            version=version,
            status=SignalStatus.ERROR,
            score=1.0,
            label="error",
            severity=Severity.HIGH,
            evidence=[Evidence(kind="detector_error", detail={"reason": reason[:120]})],
            explanation_key="risk.detector_error",
        )


# ------------------------------------------------------------------ context


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Another record something was seen on (for example a document of another invoice)."""

    entity_type: str
    entity_id: str
    workflow: str | None = None


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything detectors may read. Built by the caller; detectors never query."""

    org_id: uuid.UUID
    workflow: str
    entity_type: str
    entity_id: str
    document: NormalizedDocument | None = None
    document_sha256: str | None = None
    extraction: ExtractionResult[Any] | None = None
    # ``scan_document`` of ``document`` (WP1.4); bound to its text by a digest.
    guardrail_report: GuardrailReport | None = None
    # Same file hash seen on these other entities (see ``service.find_hash_matches``).
    hash_matches: Sequence[EntityRef] = ()
    # The amount under review and past amounts of the same kind (same vendor, ...).
    amount: Decimal | None = None
    amount_history: Sequence[Decimal] = ()
    # Workflow specific facts for workflow detectors.
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DetectorTools:
    """What a detector that declares ``needs_model`` may use."""

    router: ModelRouter | None = None


class RiskDetector(Protocol):
    """A pluggable detector. ``name`` is ``<namespace>.<detector>``."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def needs_model(self) -> bool: ...

    async def detect(self, ctx: RiskContext, tools: DetectorTools) -> RiskSignal: ...

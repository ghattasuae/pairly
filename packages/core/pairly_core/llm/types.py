# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Public data types of the model router."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

_REF_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")


class CallStatus(StrEnum):
    """Outcome of one attempt, as stored in ``model_calls.status``."""

    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    LOW_CONFIDENCE = "low_confidence"
    PROVIDER_ERROR = "provider_error"
    # Part of the work was done (and billed by the provider) before the call failed.
    PARTIAL_ERROR = "partial_error"


@dataclass(frozen=True, slots=True)
class PartialUsage:
    """What a failed call had already consumed (and the provider billed) when it failed.

    An exception carries it as ``partial_usage`` (for example a batched embedding call
    whose later batch failed). ``input_tokens`` is what the provider reported for the
    successful part, ``input_bound`` an upper bound for it (UTF-8 bytes) used when the
    provider reported nothing, ``completed`` how many items were done.
    """

    input_tokens: int = 0
    input_bound: int = 0
    completed: int = 0


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """What to run: a named task, its prompt version and its acceptance rules.

    ``confidence_threshold``: when set, the output must carry a number in [0, 1] in
    ``confidence_field``; below the threshold the router escalates to the next tier, and
    if no tier is confident enough the call fails closed (``ModelUnavailable``).
    ``max_tier``: highest tier to escalate to (default: the catalog's highest).
    """

    name: str
    prompt_version: str
    confidence_threshold: float | None = None
    confidence_field: str = "confidence"
    max_tier: str | None = None

    def __post_init__(self) -> None:
        if self.confidence_threshold is not None and not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who and what a call is for. Stored on every ``model_calls`` row (ids only, no PII)."""

    actor_id: str | None = None
    workflow: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("actor_id", "workflow", "entity_type", "entity_id", "request_id"):
            value = getattr(self, name)
            if value is not None and not _REF_RE.fullmatch(value):
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class AttemptInfo:
    """One step of the attempt chain. ``call_id`` is None when nothing was sent
    (provider unavailable, circuit open, recording missing)."""

    provider: str
    model: str
    tier: str
    outcome: str
    call_id: uuid.UUID | None = None
    error_kind: str | None = None
    cost_micros: int = 0
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class RouterResult[T: BaseModel]:
    """A validated output plus the metadata every decision record needs."""

    value: T
    call_id: uuid.UUID
    chain_id: uuid.UUID
    task: str
    prompt_version: str
    prompt_hash: str
    provider: str
    model: str
    tier: str
    request_hash: str
    input_tokens: int
    output_tokens: int
    # Cost of the accepted call and of the whole chain (escalations included), micro-USD.
    cost_micros: int
    total_cost_micros: int
    latency_ms: int
    confidence: float | None
    from_recording: bool
    attempts: tuple[AttemptInfo, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DiffEntry:
    path: str
    original: Any
    replayed: Any


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Outcome of re-running a stored call. ``matches`` is True only when the replayed
    output validated and equals the original output exactly."""

    original_call_id: uuid.UUID
    replay_call_id: uuid.UUID | None
    source: str
    matches: bool
    prompt_unchanged: bool
    schema_unchanged: bool
    original_output: Any
    replayed_output: Any
    replayed_status: str
    diff: tuple[DiffEntry, ...]

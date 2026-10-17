# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tracing for model calls: LangFuse (optional) and OpenTelemetry spans.

Nothing raw leaves the process: every input and output sent to LangFuse goes through
a ``Redactor`` first. The router and ``build_router`` default to
``pairly_core.guardrails.pii.PiiRedactor`` (emails, phones including UAE formats,
IBANs, Luhn-checked card numbers, Emirates ID, passport numbers, known names).
``RegexRedactor`` below is the older minimal redactor, kept for callers that pass it.
LangFuse also gets the redactor as its ``mask`` function, a second layer for anything
the SDK captures on its own. What the first layer masks is counted per organisation
(``pairly_core.guardrails.redaction_counts``); the second layer is not counted, since it
only sees text that was already redacted.

OpenTelemetry spans carry ids, model names, token counts and cost only, never inputs
or outputs.

Tracing is best effort: a tracing failure is logged and never fails a model call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from opentelemetry import trace

from pairly_core.llm.settings import LLMSettings

log = structlog.get_logger("pairly_core.llm.tracing")
otel_tracer = trace.get_tracer("pairly_core.llm")


class Redactor(Protocol):
    def redact(self, value: Any) -> Any:
        """Return a copy of ``value`` (JSON-like) with personal data masked."""
        ...


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Country code then 2-34 alphanumerics, optionally in groups of 4 (checked before phones).
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
# Optional +, then 8 to 15 digits with common separators.
_PHONE = re.compile(r"(?<![\w])\+?\d(?:[\s().-]?\d){7,14}(?![\w])")


class RegexRedactor:
    """Default redactor: emails, IBANs, phone numbers. Recurses into dicts and lists."""

    def redact_text(self, text: str) -> str:
        text = _EMAIL.sub("[EMAIL]", text)
        text = _IBAN.sub("[IBAN]", text)
        return _PHONE.sub("[PHONE]", text)

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact(v) for v in value]
        return value


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    """One model attempt as traced. ``input`` and ``output`` are redacted before export."""

    name: str
    chain_id: str
    call_id: str | None
    org_id: str
    task: str
    prompt_version: str
    prompt_hash: str
    provider: str
    model: str
    tier: str
    status: str
    input: Any
    output: Any
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    from_recording: bool
    error_kind: str | None = None


class Tracer(Protocol):
    def record_generation(self, record: GenerationRecord) -> None: ...

    def flush(self) -> None: ...


class NoopTracer:
    def record_generation(self, record: GenerationRecord) -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer:
    """Sends one LangFuse generation per attempt. ``client`` is injectable for tests."""

    def __init__(self, client: Any, redactor: Redactor) -> None:
        self._client = client
        self._redactor = redactor

    @classmethod
    def from_settings(cls, settings: LLMSettings, redactor: Redactor) -> LangfuseTracer:
        from langfuse import Langfuse  # imported only when LangFuse is configured

        assert settings.langfuse_public_key is not None  # noqa: S101
        assert settings.langfuse_secret_key is not None  # noqa: S101

        def mask(*, data: Any, **_: Any) -> Any:
            return redactor.redact(data)

        client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            host=settings.langfuse_host,
            mask=mask,
        )
        return cls(client, redactor)

    def _redact(self, value: Any, org_id: str) -> Any:
        counted = getattr(self._redactor, "redact_counted", None)
        if counted is None:
            return self._redactor.redact(value)
        redacted, n = counted(value)
        if n:
            # Counted for the AI governance board (numbers only).
            from pairly_core.guardrails.redaction_counts import TALLY

            TALLY.add(org_id, n)
        return redacted

    def record_generation(self, record: GenerationRecord) -> None:
        try:
            generation = self._client.start_observation(
                name=record.name,
                as_type="generation",
                model=record.model,
                input=self._redact(record.input, record.org_id),
                metadata={
                    "chain_id": record.chain_id,
                    "call_id": record.call_id,
                    "org_id": record.org_id,
                    "task": record.task,
                    "prompt_hash": record.prompt_hash,
                    "provider": record.provider,
                    "tier": record.tier,
                    "status": record.status,
                    "from_recording": record.from_recording,
                    "error_kind": record.error_kind,
                    "latency_ms": record.latency_ms,
                },
                version=record.prompt_version,
                level="DEFAULT" if record.status == "ok" else "WARNING",
            )
            generation.update(
                output=self._redact(record.output, record.org_id),
                usage_details={
                    "input": record.input_tokens,
                    "output": record.output_tokens,
                },
                cost_details={"total": record.cost_micros / 1_000_000},
            )
            generation.end()
        except Exception as exc:  # tracing must never break a call
            log.warning("llm.trace_failed", error=type(exc).__name__)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            log.warning("llm.trace_flush_failed", error=type(exc).__name__)


def build_tracer(settings: LLMSettings, redactor: Redactor) -> Tracer:
    if not settings.langfuse_enabled:
        return NoopTracer()
    try:
        return LangfuseTracer.from_settings(settings, redactor)
    except Exception as exc:
        log.warning("llm.langfuse_unavailable", error=type(exc).__name__)
        return NoopTracer()

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Errors raised by the model router.

Fail closed (brief hard rule 4): every error a caller can receive from
``ModelRouter.complete`` is a ``ModelUnavailable`` (or a subclass). Callers must treat
it as "the check could not run" and route the item to a human queue. It is never a
reason to approve, accept or skip a step.

``ProviderError`` is internal: providers raise it, the router classifies it (retry,
fall back, escalate) and never lets it reach a caller.
"""

from __future__ import annotations

from enum import StrEnum

from pairly_core.errors import ServiceUnavailableError


class ModelUnavailable(ServiceUnavailableError):  # noqa: N818  (name fixed by the contract)
    """No validated model output could be produced. Fail closed: route to a human."""

    code = "model_unavailable"
    title = "Model unavailable"


class RecordingMissing(ModelUnavailable):
    """``recorded`` mode and no stored response matches the request."""

    code = "model_recording_missing"
    title = "No recorded model response"


class BudgetExceeded(ModelUnavailable):
    """The organisation's monthly model budget cannot cover the next call."""

    code = "model_budget_exceeded"
    title = "Model budget exceeded"


class InputTooLarge(ModelUnavailable):
    """The request is larger than the router stores, so it is refused before any call."""

    code = "model_input_too_large"
    title = "Model input too large"


class ErrorKind(StrEnum):
    """Why one provider request failed. Drives retry, breaker and fallback decisions."""

    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


_RETRYABLE = frozenset(
    {ErrorKind.RATE_LIMITED, ErrorKind.SERVER_ERROR, ErrorKind.TIMEOUT, ErrorKind.CONNECTION}
)
# Failures that say "this provider is not usable right now". Bad requests are our own
# fault and say nothing about the provider's health, so they do not trip the breaker.
_BREAKER = _RETRYABLE | {ErrorKind.AUTH}


class ProviderError(Exception):
    """One provider request failed. ``detail`` must never contain prompt content."""

    def __init__(self, kind: ErrorKind, detail: str = "", *, status: int | None = None) -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE

    @property
    def counts_for_breaker(self) -> bool:
        return self.kind in _BREAKER

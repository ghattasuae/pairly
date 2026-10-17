# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The provider interface. Providers send one request and report what came back.

A provider never retries (the router does, with its own policy), never validates the
output against the schema (the router does) and never raises anything but
``ProviderError`` for a failed request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from pairly_core.llm.catalog import Usage


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    model: str
    system: str
    user: str
    schema: type[BaseModel]
    json_schema: dict[str, Any]
    max_output_tokens: int
    # None: the model does not accept a temperature, so none is sent.
    temperature: float | None
    effort: str | None
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """What the model returned. ``text`` is the raw output (expected to be JSON).

    ``complete`` is False when the provider says the output was cut off or refused
    (``max_tokens``, ``refusal``, ``incomplete``); the router then treats it as invalid.
    """

    text: str
    usage: Usage
    complete: bool = True
    stop_reason: str | None = None
    provider_request_id: str | None = None
    model: str | None = None


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def available(self) -> bool:
        """False when the provider cannot be used at all (for example, no API key)."""
        ...

    async def generate(self, request: ProviderRequest) -> ProviderResponse: ...

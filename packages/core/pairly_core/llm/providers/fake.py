# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A scripted provider for tests and offline development. It never touches a network.

Script per model id: a list of steps consumed in order. A step is

* a ``dict`` or ``list``: returned as JSON text,
* a ``str``: returned as raw text (use it for invalid JSON),
* a ``FakeReply``: full control over text, usage and completeness,
* a ``ProviderError`` (or any exception): raised.

When a model's script runs out, the last step repeats (so a single-step script answers
every call). A model with no script raises ``ProviderError(NOT_FOUND)``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pairly_core.llm.catalog import Usage
from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.providers.base import ProviderRequest, ProviderResponse


@dataclass(frozen=True, slots=True)
class FakeReply:
    text: str
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=100, output_tokens=20))
    complete: bool = True
    stop_reason: str | None = "end_turn"


Step = Mapping[str, Any] | Sequence[Any] | str | FakeReply | BaseException


class FakeProvider:
    def __init__(
        self,
        name: str = "fake",
        script: Mapping[str, Sequence[Step]] | None = None,
        *,
        available: bool = True,
        usage: Usage | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._usage = usage or Usage(input_tokens=100, output_tokens=20)
        self._script: dict[str, list[Step]] = {k: list(v) for k, v in (script or {}).items()}
        self._position: dict[str, int] = {}
        self.requests: list[ProviderRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, value: bool) -> None:
        self._available = value

    def script(self, model: str, *steps: Step) -> None:
        self._script[model] = list(steps)
        self._position.pop(model, None)

    def calls_for(self, model: str) -> int:
        return sum(1 for r in self.requests if r.model == model)

    async def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        steps = self._script.get(request.model)
        if not steps:
            raise ProviderError(ErrorKind.NOT_FOUND, f"no script for {request.model}")
        index = self._position.get(request.model, 0)
        self._position[request.model] = index + 1
        step = steps[min(index, len(steps) - 1)]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, FakeReply):
            return ProviderResponse(
                text=step.text,
                usage=step.usage,
                complete=step.complete,
                stop_reason=step.stop_reason,
                provider_request_id=f"fake-{len(self.requests)}",
                model=request.model,
            )
        text = step if isinstance(step, str) else json.dumps(step)
        return ProviderResponse(
            text=text,
            usage=self._usage,
            stop_reason="end_turn",
            provider_request_id=f"fake-{len(self.requests)}",
            model=request.model,
        )

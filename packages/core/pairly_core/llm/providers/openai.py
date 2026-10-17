# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""OpenAI provider (official ``openai`` SDK, Apache-2.0), Responses API.

Structured output uses ``text.format`` with ``type: json_schema`` and ``strict: true``.
The strict schema comes from the SDK's Pydantic converter (the same one
``responses.parse`` uses). The router validates the full Pydantic model afterwards.

The SDK's own retries are off (``max_retries=0``); the router retries. The client is
built only with an explicit key, so the SDK never falls back to ambient credentials.
"""

from __future__ import annotations

from typing import Any

import httpx2
import openai
from openai.lib._pydantic import to_strict_json_schema

from pairly_core.llm.catalog import Usage
from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.providers.base import ProviderRequest, ProviderResponse

PROVIDER_NAME = "openai"


def classify_openai_error(exc: BaseException) -> ProviderError:
    """Map an SDK exception to a ``ProviderError`` (same policy as Anthropic)."""
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ErrorKind.TIMEOUT, "request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(ErrorKind.CONNECTION, "connection failed")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if isinstance(exc, openai.RateLimitError) or status == 429:
            kind = ErrorKind.RATE_LIMITED
        elif isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            kind = ErrorKind.AUTH
        elif isinstance(exc, openai.NotFoundError):
            kind = ErrorKind.NOT_FOUND
        elif status in (408, 409) or status >= 500:
            kind = ErrorKind.SERVER_ERROR
        else:
            kind = ErrorKind.BAD_REQUEST
        return ProviderError(kind, f"HTTP {status}", status=status)
    return ProviderError(ErrorKind.UNKNOWN, type(exc).__name__)


class OpenAIProvider:
    def __init__(
        self,
        api_key: str | None,
        *,
        timeout_seconds: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client: openai.AsyncOpenAI | None = None
        if api_key:
            self._client = openai.AsyncOpenAI(
                api_key=api_key,
                max_retries=0,
                timeout=timeout_seconds,
                http_client=http_client,
                base_url=base_url,
            )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def available(self) -> bool:
        return self._client is not None

    def build_params(self, request: ProviderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": [{"role": "user", "content": request.user}],
            "max_output_tokens": request.max_output_tokens,
            # Do not keep the request on OpenAI's side for later retrieval.
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema.__name__[:64],
                    "schema": to_strict_json_schema(request.schema),
                    "strict": True,
                }
            },
        }
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.effort:
            params["reasoning"] = {"effort": request.effort}
        return params

    async def generate(self, request: ProviderRequest) -> ProviderResponse:
        if self._client is None:
            raise ProviderError(ErrorKind.UNAVAILABLE, "no API key configured")
        try:
            response = await self._client.responses.create(
                **self.build_params(request), timeout=request.timeout_seconds
            )
        except openai.OpenAIError as exc:
            raise classify_openai_error(exc) from None
        refused = any(
            getattr(part, "type", None) == "refusal"
            for item in response.output
            if item.type == "message"
            for part in item.content
        )
        usage = response.usage
        cached = 0
        input_tokens = output_tokens = 0
        if usage is not None:
            cached = usage.input_tokens_details.cached_tokens or 0
            input_tokens = max(usage.input_tokens - cached, 0)
            output_tokens = usage.output_tokens
        return ProviderResponse(
            text=response.output_text,
            usage=Usage(
                input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cached
            ),
            complete=response.status == "completed" and not refused,
            stop_reason="refusal" if refused else response.status,
            provider_request_id=response.id,
            model=response.model,
        )

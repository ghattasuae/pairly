# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Anthropic Claude provider (official ``anthropic`` SDK, MIT).

Structured output uses the Messages API ``output_config.format`` with a JSON schema
(``type: json_schema``), built from the Pydantic class with the SDK's public
``transform_schema`` helper (it moves constraints the API does not accept into
descriptions; the router still validates the full Pydantic model afterwards).
Forced ``tool_choice`` is not used: current models reject it.

The SDK's own retries are off (``max_retries=0``); the router retries. The client is
built only with an explicit key, so the SDK never falls back to ambient credentials.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2

from pairly_core.llm.catalog import Usage
from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.providers.base import ProviderRequest, ProviderResponse

PROVIDER_NAME = "anthropic"
_INCOMPLETE_STOPS = frozenset(
    {"max_tokens", "refusal", "pause_turn", "model_context_window_exceeded"}
)


def classify_anthropic_error(exc: BaseException) -> ProviderError:
    """Map an SDK exception to a ``ProviderError``. Retryable: 429, 408, 409, >= 500,
    timeouts and connection errors. Everything else is not retried."""
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(ErrorKind.TIMEOUT, "request timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(ErrorKind.CONNECTION, "connection failed")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if isinstance(exc, anthropic.RateLimitError) or status == 429:
            kind = ErrorKind.RATE_LIMITED
        elif isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            kind = ErrorKind.AUTH
        elif isinstance(exc, anthropic.NotFoundError):
            kind = ErrorKind.NOT_FOUND
        elif status in (408, 409) or status >= 500:
            kind = ErrorKind.SERVER_ERROR
        else:
            kind = ErrorKind.BAD_REQUEST
        return ProviderError(kind, f"HTTP {status}", status=status)
    return ProviderError(ErrorKind.UNKNOWN, type(exc).__name__)


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None,
        *,
        timeout_seconds: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        if api_key:
            self._client = anthropic.AsyncAnthropic(
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
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(request.json_schema),
            }
        }
        if request.effort:
            output_config["effort"] = request.effort
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": output_config,
        }
        if request.temperature is not None:
            # SDK 1.x has no typed temperature argument; the API still accepts it on
            # models that support sampling settings (Haiku 4.5).
            params["extra_body"] = {"temperature": request.temperature}
        return params

    async def generate(self, request: ProviderRequest) -> ProviderResponse:
        if self._client is None:
            raise ProviderError(ErrorKind.UNAVAILABLE, "no API key configured")
        try:
            message = await self._client.messages.create(
                **self.build_params(request), timeout=request.timeout_seconds
            )
        except anthropic.AnthropicError as exc:
            raise classify_anthropic_error(exc) from None
        text = next((block.text for block in message.content if block.type == "text"), "")
        usage = message.usage
        return ProviderResponse(
            text=text,
            usage=Usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            complete=message.stop_reason not in _INCOMPLETE_STOPS,
            stop_reason=message.stop_reason,
            provider_request_id=message._request_id,
            model=message.model,
        )

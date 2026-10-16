# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Provider adapters against the real SDKs, with a mocked HTTP transport.

No request leaves the process: every test passes an ``httpx2.MockTransport``. These
tests pin the request shape we send (structured output, no forced tool choice,
temperature only where supported) and the retry classification of errors.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel, Field, SecretStr

from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.factory import build_providers
from pairly_core.llm.providers.anthropic import AnthropicProvider, classify_anthropic_error
from pairly_core.llm.providers.base import ModelProvider, ProviderRequest
from pairly_core.llm.providers.openai import OpenAIProvider, classify_openai_error
from pairly_core.llm.settings import LLMSettings


class Out(BaseModel):
    label: str = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)


def _request(
    model: str, temperature: float | None = 0.0, effort: str | None = None
) -> ProviderRequest:
    return ProviderRequest(
        model=model,
        system="System prompt",
        user="User data",
        schema=Out,
        json_schema=Out.model_json_schema(),
        max_output_tokens=256,
        temperature=temperature,
        effort=effort,
        timeout_seconds=5.0,
    )


Handler = Callable[[httpx2.Request], httpx2.Response]


def _client(handler: Handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _anthropic_message(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 50,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        },
    }


async def test_anthropic_request_shape_and_response() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(
            200,
            json=_anthropic_message('{"label":"a","confidence":0.5}'),
            headers={"request-id": "req_123"},
        )

    provider = AnthropicProvider("test-key", http_client=_client(handler))
    assert provider.available
    assert provider.name == "anthropic"
    response = await provider.generate(_request("claude-haiku-4-5-20251001", effort="low"))
    body = seen[0]["body"]
    assert seen[0]["url"].endswith("/v1/messages")
    assert seen[0]["headers"]["x-api-key"] == "test-key"
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["system"] == "System prompt"
    assert body["messages"] == [{"role": "user", "content": "User data"}]
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.0
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert "tool_choice" not in body
    assert response.text == '{"label":"a","confidence":0.5}'
    assert response.complete
    assert (response.usage.input_tokens, response.usage.output_tokens) == (50, 7)
    assert (response.usage.cache_read_tokens, response.usage.cache_write_tokens) == (3, 2)
    assert response.provider_request_id == "req_123"


async def test_anthropic_no_temperature_and_incomplete_stop() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_anthropic_message("{}", stop="refusal"))

    provider = AnthropicProvider("k", http_client=_client(handler))
    response = await provider.generate(_request("claude-sonnet-5-5", temperature=None))
    assert "temperature" not in bodies[0]
    assert "effort" not in bodies[0]["output_config"]
    assert not response.complete
    assert response.stop_reason == "refusal"


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, ErrorKind.RATE_LIMITED, True),
        (500, ErrorKind.SERVER_ERROR, True),
        (529, ErrorKind.SERVER_ERROR, True),
        (408, ErrorKind.SERVER_ERROR, True),
        (409, ErrorKind.SERVER_ERROR, True),
        (400, ErrorKind.BAD_REQUEST, False),
        (413, ErrorKind.BAD_REQUEST, False),
        (422, ErrorKind.BAD_REQUEST, False),
        (401, ErrorKind.AUTH, False),
        (403, ErrorKind.AUTH, False),
        (404, ErrorKind.NOT_FOUND, False),
    ],
)
async def test_http_error_classification(status: int, kind: ErrorKind, retryable: bool) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "m"}}
        )

    for provider in (
        AnthropicProvider("k", http_client=_client(handler)),
        OpenAIProvider("k", http_client=_client(handler)),
    ):
        model = "claude-haiku-4-5-20251001" if provider.name == "anthropic" else "gpt-6-luna"
        with pytest.raises(ProviderError) as info:
            await provider.generate(_request(model))
        assert info.value.kind is kind, provider.name
        assert info.value.retryable is retryable
        assert info.value.status == status
        # The error never carries the prompt.
        assert "User data" not in str(info.value)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx2.ConnectError("refused"), ErrorKind.CONNECTION),
        (httpx2.ReadTimeout("slow"), ErrorKind.TIMEOUT),
    ],
)
async def test_network_error_classification(exc: Exception, kind: ErrorKind) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    for provider in (
        AnthropicProvider("k", http_client=_client(handler)),
        OpenAIProvider("k", http_client=_client(handler)),
    ):
        with pytest.raises(ProviderError) as info:
            await provider.generate(_request("m"))
        assert info.value.kind is kind
        assert info.value.retryable


def test_unknown_exceptions_are_not_retryable() -> None:
    assert classify_anthropic_error(RuntimeError("x")).kind is ErrorKind.UNKNOWN
    assert not classify_openai_error(RuntimeError("x")).retryable


def _openai_response(text: str, status: str = "completed", refusal: bool = False) -> dict[str, Any]:
    part: dict[str, Any] = (
        {"type": "refusal", "refusal": "no"}
        if refusal
        else {"type": "output_text", "text": text, "annotations": []}
    )
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-6-luna",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 50,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 55,
        },
    }


async def test_openai_request_shape_and_response() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers["authorization"],
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(200, json=_openai_response('{"label":"a","confidence":0.5}'))

    provider = OpenAIProvider("sk-test", http_client=_client(handler))
    assert provider.name == "openai"
    response = await provider.generate(_request("gpt-6-luna", temperature=None, effort="low"))
    body = seen[0]["body"]
    assert seen[0]["url"].endswith("/responses")
    assert seen[0]["auth"] == "Bearer sk-test"
    assert body["model"] == "gpt-6-luna"
    assert body["instructions"] == "System prompt"
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["name"] == "Out"
    assert body["reasoning"] == {"effort": "low"}
    assert "temperature" not in body
    assert response.text == '{"label":"a","confidence":0.5}'
    assert response.complete
    # Cached tokens are split out of input tokens.
    assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (40, 10)
    assert response.usage.output_tokens == 5


async def test_openai_refusal_and_incomplete() -> None:
    replies = iter(
        [_openai_response("", refusal=True), _openai_response("{}", status="incomplete")]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=next(replies))

    provider = OpenAIProvider("k", http_client=_client(handler))
    refused = await provider.generate(_request("gpt-6-luna", temperature=0.0))
    assert not refused.complete
    assert refused.stop_reason == "refusal"
    cut = await provider.generate(_request("gpt-6-luna"))
    assert not cut.complete


async def test_providers_without_keys_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ambient keys must never be picked up by an SDK behind our back.
    providers = build_providers(LLMSettings(anthropic_api_key=None, openai_api_key=None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-should-not-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-should-not-be-used")
    for provider in providers.values():
        assert isinstance(provider, ModelProvider)
        assert not provider.available
        with pytest.raises(ProviderError) as info:
            await provider.generate(_request("m"))
        assert info.value.kind is ErrorKind.UNAVAILABLE


def test_providers_with_keys_are_available() -> None:
    providers = build_providers(
        LLMSettings(anthropic_api_key=SecretStr("a"), openai_api_key=SecretStr("b"))
    )
    assert all(p.available for p in providers.values())

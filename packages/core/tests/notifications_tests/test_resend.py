# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Resend transport against a fake HTTP layer (``httpx.MockTransport``).

Checked against https://resend.com/docs/api-reference/emails/send-email,
https://resend.com/docs/api-reference/errors and the rate limit and idempotency pages.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pairly_core.config import ConfigError
from pairly_core.notifications.email import (
    DeliveryError,
    OutgoingEmail,
    ResendTransport,
    email_transport_from_settings,
    resend_idempotency_key,
    resend_tags,
)
from pairly_core.notifications.settings import load_notification_settings

PLACEHOLDER_KEY = "re_placeholder_not_a_real_key"


def _email(**kw: Any) -> OutgoingEmail:
    values: dict[str, Any] = {
        "to": "person@example.test",
        "sender": "Pairly <no-reply@mail.example.test>",
        "subject": "Approval needed",
        "text": "Plain text",
        "html": "<p>x</p>",
        "locale": "en",
        "message_id": "abc@mail.example.test",
        "idempotency_key": "org:key",
    }
    values.update(kw)
    return OutgoingEmail(**values)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_request_shape_reply_to_and_tags() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"})

    async with _client(handler) as client:
        transport = ResendTransport(api_key=PLACEHOLDER_KEY, client=client)
        provider_id = await transport.send(
            _email(
                reply_to="AP team <ap@example.test>",
                tags=(("category", "approval_requested"), ("bad tag", "x"), ("k", "a.b")),
            )
        )
    assert provider_id == "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == f"Bearer {PLACEHOLDER_KEY}"
    assert request.headers["Idempotency-Key"] == "org:key"
    body = json.loads(request.content)
    assert body["from"] == "Pairly <no-reply@mail.example.test>"
    assert body["to"] == ["person@example.test"]
    assert body["reply_to"] == ["AP team <ap@example.test>"]
    # Only pairs that match Resend's tag rules are sent.
    assert body["tags"] == [{"name": "category", "value": "approval_requested"}]
    assert body["headers"]["Auto-Submitted"] == "auto-generated"
    assert "Reply-To" not in body["headers"]


def test_idempotency_key_rules() -> None:
    assert resend_idempotency_key("org:abc") == "org:abc"
    long_key = "k" * 300
    hashed = resend_idempotency_key(long_key)
    assert hashed.startswith("sha256-")
    assert len(hashed) <= 256
    # Stable: the same notification always sends the same key.
    assert resend_idempotency_key(long_key) == hashed
    assert resend_idempotency_key("a\r\nb") == "a b"
    with pytest.raises(DeliveryError) as exc:
        resend_idempotency_key("  ")
    assert exc.value.transient is False


def test_tags_are_limited() -> None:
    many = tuple((f"t{i}", "v") for i in range(10))
    assert len(resend_tags(many)) == 5
    assert resend_tags((("x" * 257, "v"),)) == []


@pytest.mark.parametrize(
    ("status", "name", "transient"),
    [
        (400, "validation_error", False),
        (400, "invalid_idempotency_key", False),
        (401, "missing_api_key", False),
        (403, "invalid_permission", False),
        (404, "not_found", False),
        (409, "invalid_idempotent_request", False),
        (409, "concurrent_idempotent_requests", True),
        (422, "invalid_attachment", False),
        (429, "daily_quota_exceeded", True),
        (429, "monthly_quota_exceeded", True),
        (500, "application_error", True),
        (503, "service_unavailable", True),
    ],
)
async def test_error_mapping(status: int, name: str, transient: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"statusCode": status, "name": name, "message": "x"},
            headers={"retry-after": "60"} if status == 429 else {},
        )

    async with _client(handler) as client:
        with pytest.raises(DeliveryError) as exc:
            await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email())
    assert exc.value.code == f"resend_{status}_{name}"
    assert exc.value.transient is transient
    if status == 429:
        assert exc.value.retry_after == 60


async def test_error_body_is_not_trusted_for_codes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, content=b"<html>proxy error</html>")

    async with _client(handler) as client:
        with pytest.raises(DeliveryError) as exc:
            await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email())
    assert exc.value.code == "resend_422"
    assert exc.value.transient is False


async def test_short_rate_limit_is_retried_once_with_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("pairly_core.notifications.email.asyncio.sleep", fake_sleep)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) == 1:
            return httpx.Response(
                429, json={"name": "rate_limit_exceeded"}, headers={"retry-after": "1"}
            )
        return httpx.Response(200, json={"id": "re_2"})

    async with _client(handler) as client:
        assert await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email()) == (
            "re_2"
        )
    assert sleeps == [1.0]
    assert keys == ["org:key", "org:key"]


async def test_long_rate_limit_is_left_to_the_delivery_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(seconds: float) -> None:
        raise AssertionError("must not sleep for a long Retry-After")

    monkeypatch.setattr("pairly_core.notifications.email.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"name": "rate_limit_exceeded"}, headers={"retry-after": "30"}
        )

    async with _client(handler) as client:
        with pytest.raises(DeliveryError) as exc:
            await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email())
    assert calls == 1
    assert exc.value.transient is True
    assert exc.value.retry_after == 30


async def test_retried_twice_at_most(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("pairly_core.notifications.email.asyncio.sleep", fake_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"name": "rate_limit_exceeded"}, headers={"retry-after": "1"}
        )

    async with _client(handler) as client:
        with pytest.raises(DeliveryError):
            await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email())
    assert calls == 2


async def test_network_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with _client(handler) as client:
        with pytest.raises(DeliveryError) as exc:
            await ResendTransport(api_key=PLACEHOLDER_KEY, client=client).send(_email())
    assert exc.value.code == "resend_unavailable"
    assert exc.value.transient is True


def _settings(**kw: Any) -> Any:
    values: dict[str, Any] = {
        "env": "production",
        "public_web_url": "https://web.example.test",
        "public_api_url": "https://api.example.test",
        "signed_link_secret": "s" * 40,
        "email_transport": "resend",
    }
    values.update(kw)
    return load_notification_settings(**values)


def test_settings_fail_closed_without_a_key() -> None:
    with pytest.raises(ConfigError, match="resend_api_key is required"):
        _settings()
    with pytest.raises(ConfigError, match="resend_api_key is required"):
        _settings(resend_api_key="   ")
    with pytest.raises(ConfigError, match="Resend API key"):
        _settings(resend_api_key="not-a-resend-key")
    with pytest.raises(ConfigError, match="resend_base_url must use https"):
        _settings(resend_api_key=PLACEHOLDER_KEY, resend_base_url="http://api.resend.com")


async def test_settings_build_the_transport() -> None:
    settings = _settings(resend_api_key=PLACEHOLDER_KEY, email_reply_to="ap@example.test")
    assert settings.email_reply_to == "ap@example.test"
    async with _client(lambda r: httpx.Response(200, json={"id": "x"})) as client:
        transport = email_transport_from_settings(settings, client=client)
        assert isinstance(transport, ResendTransport)
        assert await transport.send(_email()) == "x"

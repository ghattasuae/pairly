# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Resend inbound: Svix-style signature checks and the Receiving API client (fake HTTP)."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from pairly_core.config import ConfigError
from pairly_core.integrations.resend.receiving import ReceivingError, ResendReceivingClient
from pairly_core.integrations.resend.signature import (
    MAX_BODY_BYTES,
    ResendSignatureError,
    decode_secret,
    sign,
    verify,
)
from pairly_core.notifications.settings import load_notification_settings

# A throwaway test secret (random bytes, never used anywhere else).
SECRET = "whsec_" + base64.b64encode(b"pairly-test-only-signing-key-0001").decode()
OTHER = "whsec_" + base64.b64encode(b"pairly-test-only-signing-key-0002").decode()
EMAIL_ID = "56761188-7520-42d8-8898-ff6fc54ce618"
BODY = b'{"type":"email.received","data":{"email_id":"' + EMAIL_ID.encode() + b'"}}'


def _now() -> str:
    return str(int(time.time()))


def test_valid_signature_returns_the_message_id() -> None:
    ts = _now()
    header = sign(SECRET, "msg_1", ts, BODY)
    assert verify(secret=SECRET, msg_id="msg_1", timestamp=ts, signature=header, body=BODY) == (
        "msg_1"
    )


def test_any_of_several_signatures_matches() -> None:
    ts = _now()
    header = f"{sign(OTHER, 'msg_1', ts, BODY)} v2,abc {sign(SECRET, 'msg_1', ts, BODY)}"
    verify(secret=SECRET, msg_id="msg_1", timestamp=ts, signature=header, body=BODY)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"secret": OTHER}, "signature_mismatch"),
        ({"body": BODY + b" "}, "signature_mismatch"),
        ({"msg_id": "msg_2"}, "signature_mismatch"),
        ({"msg_id": None}, "bad_id"),
        ({"msg_id": "../x"}, "bad_id"),
        ({"timestamp": None}, "bad_timestamp"),
        ({"timestamp": "12a"}, "bad_timestamp"),
        ({"timestamp": str(int(time.time()) - 900)}, "stale_timestamp"),
        ({"timestamp": str(int(time.time()) + 900)}, "stale_timestamp"),
        ({"signature": None}, "bad_signature"),
        ({"signature": "v1,é"}, "bad_signature"),
        ({"secret": "not-a-secret"}, "no_secret"),
        ({"body": b"x" * (MAX_BODY_BYTES + 1)}, "body_too_large"),
    ],
)
def test_bad_requests_are_refused(change: dict[str, object], reason: str) -> None:
    ts = _now()
    args: dict[str, object] = {
        "secret": SECRET,
        "msg_id": "msg_1",
        "timestamp": ts,
        "signature": sign(SECRET, "msg_1", ts, BODY),
        "body": BODY,
    }
    args.update(change)
    if "timestamp" in change and change["timestamp"] is not None and reason != "bad_timestamp":
        # A stale timestamp with a signature made for it: only the clock check refuses.
        args["signature"] = sign(SECRET, "msg_1", str(change["timestamp"]), BODY)
    with pytest.raises(ResendSignatureError) as exc:
        verify(**args)  # type: ignore[arg-type]
    assert exc.value.reason == reason


def test_secret_decoding() -> None:
    assert decode_secret(SECRET) == b"pairly-test-only-signing-key-0001"
    for bad in ("abc", "whsec_!!!", "whsec_" + base64.b64encode(b"short").decode()):
        with pytest.raises(ValueError, match="secret"):
            decode_secret(bad)


def _settings(**kw: object) -> object:
    values: dict[str, object] = {
        "env": "production",
        "public_web_url": "https://web.example.test",
        "public_api_url": "https://api.example.test",
        "signed_link_secret": "s" * 40,
        "email_transport": "resend",
        "resend_api_key": "re_placeholder_send",
    }
    values.update(kw)
    return load_notification_settings(**values)


def test_inbound_settings_come_in_pairs() -> None:
    _settings()
    # Blank values (an empty line in an env file) mean unset, not an invalid secret.
    blank: dict[str, object] = {
        "resend_webhook_secret": " ",
        "resend_inbound_api_key": "",
        "email_reply_to": "",
    }
    _settings(**blank)
    _settings(resend_webhook_secret=SECRET, resend_inbound_api_key="re_placeholder_full")
    with pytest.raises(ConfigError, match="needed together"):
        _settings(resend_webhook_secret=SECRET)
    with pytest.raises(ConfigError, match="needed together"):
        _settings(resend_inbound_api_key="re_placeholder_full")
    with pytest.raises(ConfigError, match="whsec_"):
        _settings(
            resend_webhook_secret="plain",  # noqa: S106 (a malformed test value)
            resend_inbound_api_key="re_placeholder_full",
        )
    with pytest.raises(ConfigError, match="resend_inbound_api_key must be"):
        _settings(resend_webhook_secret=SECRET, resend_inbound_api_key="sk_wrong")


# ------------------------------------------------------------------ Receiving API


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _api(download: str) -> Callable[[httpx.Request], httpx.Response | None]:
    def answer(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == "api.resend.com":
            assert request.url.path == f"/emails/receiving/{EMAIL_ID}"
            assert request.headers["Authorization"] == "Bearer re_placeholder_full"
            return httpx.Response(200, json={"id": EMAIL_ID, "raw": {"download_url": download}})
        return None

    return answer


async def test_raw_message_follows_https_redirects() -> None:
    api = _api("https://inbound.resend.example/raw/1")

    def handler(request: httpx.Request) -> httpx.Response:
        found = api(request)
        if found is not None:
            return found
        if request.url.path == "/raw/1":
            return httpx.Response(302, headers={"location": "https://cdn.resend.example/raw/2"})
        return httpx.Response(200, content=b"From: a@example.test\r\n\r\nhi")

    async with _client(handler) as http:
        raw = await ResendReceivingClient(api_key="re_placeholder_full", client=http).raw_message(
            EMAIL_ID, max_bytes=1024
        )
    assert raw.startswith(b"From: a@example.test")


@pytest.mark.parametrize(
    ("download", "redirect", "code"),
    [
        ("http://inbound.resend.example/raw", None, "no_raw_download"),
        (
            "https://inbound.resend.example/raw",
            "http://evil.example/x",
            "download_redirect_refused",
        ),
    ],
)
async def test_plain_http_is_refused(download: str, redirect: str | None, code: str) -> None:
    api = _api(download)

    def handler(request: httpx.Request) -> httpx.Response:
        found = api(request)
        if found is not None:
            return found
        return httpx.Response(302, headers={"location": redirect or ""})

    async with _client(handler) as http:
        with pytest.raises(ReceivingError) as exc:
            await ResendReceivingClient(api_key="re_placeholder_full", client=http).raw_message(
                EMAIL_ID, max_bytes=1024
            )
    assert exc.value.code == code
    assert exc.value.transient is False


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.parametrize("declared", [True, False])
async def test_size_limit(declared: bool) -> None:
    api = _api("https://inbound.resend.example/raw")
    big = b"x" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        found = api(request)
        if found is not None:
            return found
        if declared:
            return httpx.Response(200, content=big)
        return httpx.Response(200, content=_chunks(big[:1000], big[1000:]))

    async with _client(handler) as http:
        with pytest.raises(ReceivingError) as exc:
            await ResendReceivingClient(api_key="re_placeholder_full", client=http).raw_message(
                EMAIL_ID, max_bytes=1024
            )
    assert exc.value.code == "too_large"


@pytest.mark.parametrize(
    ("status", "transient"), [(404, False), (401, False), (429, True), (503, True)]
)
async def test_api_errors(status: int, transient: bool) -> None:
    async with _client(lambda r: httpx.Response(status, json={"name": "x"})) as http:
        with pytest.raises(ReceivingError) as exc:
            await ResendReceivingClient(api_key="re_placeholder_full", client=http).raw_message(
                EMAIL_ID, max_bytes=1024
            )
    assert exc.value.transient is transient


async def test_bad_email_id_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(handler) as http:
        with pytest.raises(ReceivingError, match="bad_email_id"):
            await ResendReceivingClient(api_key="re_placeholder_full", client=http).raw_message(
                "../../domains", max_bytes=1024
            )

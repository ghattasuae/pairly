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

API_KEY = "re_placeholder_full"
# A fake DNS: names to addresses (no real lookup). Documentation ranges such as
# 203.0.113.0/24 are not global, so public-looking addresses stand in for public hosts.
DNS = {
    "inbound.resend.example": ["93.184.215.14"],
    "cdn.resend.example": ["151.101.1.1"],
    "loopback.example": ["127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "private.example": ["10.0.0.5"],
    "nat64.example": ["64:ff9b::a9fe:a9fe"],
    "mixed.example": ["151.101.65.1", "192.168.1.1"],
}


async def fake_resolver(host: str, port: int) -> list[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def _api_client(download_url: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.resend.com"
        assert request.url.path == f"/emails/receiving/{EMAIL_ID}"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(200, json={"id": EMAIL_ID, "raw": {"download_url": download_url}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _receiver(
    download_url: str, handler: Callable[[httpx.Request], httpx.Response]
) -> ResendReceivingClient:
    return ResendReceivingClient(
        api_key=API_KEY,
        client=_api_client(download_url),
        resolver=fake_resolver,
        download_transport=httpx.MockTransport(handler),
    )


async def _raw(receiver: ResendReceivingClient, max_bytes: int = 1024) -> bytes:
    try:
        return await receiver.raw_message(EMAIL_ID, max_bytes=max_bytes)
    finally:
        await receiver.aclose()


async def test_public_download_goes_to_the_checked_address_without_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/raw/1":
            return httpx.Response(302, headers={"location": "https://cdn.resend.example/raw/2"})
        return httpx.Response(200, stream=httpx.ByteStream(b"From: a@example.test\r\n\r\nhi"))

    raw = await _raw(_receiver("https://inbound.resend.example/raw/1", handler))
    assert raw.startswith(b"From: a@example.test")
    # Pinned to the resolved address; the name stays in Host and TLS SNI.
    assert [r.url.host for r in seen] == ["93.184.215.14", "151.101.1.1"]
    assert [r.headers["Host"] for r in seen] == ["inbound.resend.example", "cdn.resend.example"]
    assert seen[0].extensions["sni_hostname"] == "inbound.resend.example"
    assert all("Authorization" not in r.headers for r in seen)
    assert all(r.headers["Accept-Encoding"] == "identity" for r in seen)


@pytest.mark.parametrize(
    "download",
    [
        "https://loopback.example/raw",
        "https://metadata.example/latest/meta-data",
        "https://private.example/raw",
        "https://nat64.example/raw",
        "https://mixed.example/raw",
        "https://127.0.0.1/raw",
        "https://[::1]/raw",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/raw",
        "https://unknown.example/raw",
    ],
)
async def test_unsafe_download_hosts_are_never_contacted(download: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"contacted {request.url}")

    with pytest.raises(ReceivingError) as exc:
        await _raw(_receiver(download, handler))
    assert exc.value.code == "unsafe_download_url"
    assert exc.value.transient is False


@pytest.mark.parametrize(
    "location",
    [
        "https://private.example/x",
        "https://metadata.example/latest/meta-data",
        "https://10.1.2.3/x",
        "http://cdn.resend.example/x",
    ],
)
async def test_redirect_to_an_unsafe_target_is_refused(location: str) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(ReceivingError) as exc:
        await _raw(_receiver("https://inbound.resend.example/raw", handler))
    assert exc.value.code in {"unsafe_download_url", "download_redirect_refused"}
    assert hosts == ["93.184.215.14"]


async def test_redirect_loop_stops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://cdn.resend.example/again"})

    with pytest.raises(ReceivingError, match="too_many_redirects"):
        await _raw(_receiver("https://inbound.resend.example/raw", handler))


async def test_compressed_body_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-encoding": "gzip"})

    with pytest.raises(ReceivingError, match="content_encoding_refused"):
        await _raw(_receiver("https://inbound.resend.example/raw", handler))


async def test_plain_http_download_url_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no download expected")

    with pytest.raises(ReceivingError, match="no_raw_download"):
        await _raw(_receiver("http://inbound.resend.example/raw", handler))


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.parametrize("declared", [True, False])
async def test_size_limit(declared: bool) -> None:
    big = b"x" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        if declared:
            return httpx.Response(
                200, headers={"content-length": str(len(big))}, stream=httpx.ByteStream(big)
            )
        return httpx.Response(200, content=_chunks(big[:1000], big[1000:]))

    with pytest.raises(ReceivingError, match="too_large"):
        await _raw(_receiver("https://inbound.resend.example/raw", handler))


@pytest.mark.parametrize(
    ("status", "transient"), [(404, False), (401, False), (429, True), (503, True)]
)
async def test_api_errors(status: int, transient: bool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"name": "x"}))
    ) as http:
        receiver = ResendReceivingClient(api_key=API_KEY, client=http, resolver=fake_resolver)
        with pytest.raises(ReceivingError) as exc:
            await _raw(receiver)
    assert exc.value.transient is transient


async def test_bad_email_id_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    receiver = _receiver("https://inbound.resend.example/raw", handler)
    try:
        with pytest.raises(ReceivingError, match="bad_email_id"):
            await receiver.raw_message("../../domains", max_bytes=1024)
    finally:
        await receiver.aclose()

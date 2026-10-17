# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Webhooks without a database: signatures, SSRF rules, retries, events, the sender."""

from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pairly_core.config import ConfigError, Environment
from pairly_core.webhooks.delivery import (
    MAX_AGE,
    RETRY_SCHEDULE,
    HttpxSender,
    OutgoingWebhook,
    retry_delay,
)
from pairly_core.webhooks.events import (
    CORE_EVENTS,
    EventRegistry,
    EventRegistryError,
    EventType,
    build_event_registry,
    envelope,
    serialise,
)
from pairly_core.webhooks.settings import load_webhook_settings
from pairly_core.webhooks.signature import (
    WebhookSignatureError,
    sign_payload,
    verify_webhook_signature,
)
from pairly_core.webhooks.ssrf import (
    UnsafeWebhookUrlError,
    is_blocked_ip,
    parse_target,
    resolve_target,
)

SECRET = "whsec_test-secret-value"  # noqa: S105 (test value)
BODY = b'{"id":"evt_1","type":"document.received"}'

# ------------------------------------------------------------------ signatures


def test_signature_round_trip_and_rotation() -> None:
    header = sign_payload(SECRET, BODY, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_webhook_signature(BODY, header, SECRET, now=1_700_000_100) == 1_700_000_000
    # A str body is the same bytes.
    assert verify_webhook_signature(BODY.decode(), header, SECRET, now=1_700_000_000)
    # During a rotation the header may carry several v1 values; one match is enough.
    other = sign_payload("whsec_old", BODY, timestamp=1_700_000_000).split(",")[1]
    assert verify_webhook_signature(BODY, f"{header},{other}", SECRET, now=1_700_000_000)


@pytest.mark.parametrize(
    ("header", "now", "secret", "body"),
    [
        (None, 1_700_000_000, SECRET, BODY),
        ("", 1_700_000_000, SECRET, BODY),
        ("garbage", 1_700_000_000, SECRET, BODY),
        ("t=abc,v1=00", 1_700_000_000, SECRET, BODY),
        ("t=1700000000", 1_700_000_000, SECRET, BODY),
        ("SIGNED", 1_700_000_301, SECRET, BODY),  # too old
        ("SIGNED", 1_699_999_699, SECRET, BODY),  # too far in the future
        ("SIGNED", 1_700_000_000, "whsec_wrong", BODY),
        ("SIGNED", 1_700_000_000, SECRET, BODY + b" "),  # body changed
        ("SIGNED", 1_700_000_000, "", BODY),
    ],
)
def test_signature_failures(header: str | None, now: int, secret: str, body: bytes) -> None:
    if header == "SIGNED":
        header = sign_payload(SECRET, BODY, timestamp=1_700_000_000)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(body, header, secret, now=now)


def test_signature_rejects_duplicate_timestamps() -> None:
    header = sign_payload(SECRET, BODY, timestamp=1_700_000_000)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(BODY, f"t=1,{header}", SECRET, now=1_700_000_000)


# ------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",  # noqa: S104 (a test value)
        "224.0.0.1",
        "198.18.0.1",
        "203.0.113.5",
        "::1",
        "::",
        "fd00:ec2::254",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:a00:1::",  # 6to4 of 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "2001:db8::1",
    ],
)
def test_blocked_addresses(address: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["93.184.215.14", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(address: str) -> None:
    assert not is_blocked_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "ftp://example.com/hook",
        "https://user:pw@example.com/hook",
        "https://example.com/hook#frag",
        "https://localhost/hook",
        "https://metadata.internal/hook",
        "https://printer.local/hook",
        "https://example.com:0/hook",
        "https://exa mple.com/hook",
        " https://example.com/hook",
        "https:///hook",
        "https://example.com:99999/",
    ],
)
def test_unsafe_url_shapes(url: str) -> None:
    with pytest.raises(UnsafeWebhookUrlError):
        parse_target(url)


def test_target_shape() -> None:
    target = parse_target("https://Hooks.Example.com./in?x=1")
    assert (target.host, target.port, target.path) == ("hooks.example.com", 443, "/in?x=1")
    assert target.host_header == "hooks.example.com"
    assert target.url_for(ipaddress.ip_address("93.184.215.14")) == (
        "https://93.184.215.14:443/in?x=1"
    )
    v6 = parse_target("https://[2606:4700::1]:8443/")
    assert v6.host_header == "[2606:4700::1]:8443"
    assert parse_target("http://example.com/", allow_http=True).port == 80


def _resolver(*answers: str) -> Any:
    async def resolve(host: str, port: int) -> list[str]:
        return list(answers)

    return resolve


async def test_resolve_target_requires_every_address_public() -> None:
    target = parse_target("https://hooks.example.com/")
    assert str(await resolve_target(target, _resolver("93.184.215.14"))) == "93.184.215.14"
    # One internal answer among public ones is enough to refuse (DNS rebinding).
    with pytest.raises(UnsafeWebhookUrlError, match="private"):
        await resolve_target(target, _resolver("93.184.215.14", "10.0.0.1"))
    with pytest.raises(UnsafeWebhookUrlError, match="does not resolve"):
        await resolve_target(target, _resolver())
    with pytest.raises(UnsafeWebhookUrlError):
        await resolve_target(parse_target("https://127.0.0.1/"), _resolver())
    # Development only: private targets allowed.
    assert await resolve_target(target, _resolver("10.0.0.1"), allow_private=True)


async def test_resolver_errors_are_refusals() -> None:
    async def broken(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(UnsafeWebhookUrlError, match="does not resolve"):
        await resolve_target(parse_target("https://nowhere.example/"), broken)


def test_relaxed_settings_are_development_only() -> None:
    with pytest.raises(ConfigError, match="development"):
        load_webhook_settings(env=Environment.PRODUCTION, webhook_allow_private_targets=True)
    with pytest.raises(ConfigError, match="development"):
        load_webhook_settings(env=Environment.STAGING, webhook_allow_http=True)
    dev = load_webhook_settings(env=Environment.DEVELOPMENT, webhook_allow_http=True)
    assert dev.webhook_allow_http


# ------------------------------------------------------------------ retries and events


def test_retry_schedule() -> None:
    assert retry_delay(0) is None
    assert retry_delay(len(RETRY_SCHEDULE) + 1) is None
    assert retry_delay(1, rng=lambda: 0.5) == 60.0
    assert retry_delay(1, rng=lambda: 0.0) == pytest.approx(54.0)
    assert retry_delay(1, rng=lambda: 1.0) == pytest.approx(66.0)
    # The schedule fits the 24 hour delivery window (jitter may push the last one out,
    # which then ends the delivery as failed).
    assert sum(RETRY_SCHEDULE) < MAX_AGE.total_seconds()


def test_event_registry_rules() -> None:
    registry = EventRegistry()
    assert [e.key for e in registry.events] == sorted(e.key for e in CORE_EVENTS)
    with pytest.raises(EventRegistryError):
        EventType("Not.Valid", "x")
    with pytest.raises(EventRegistryError):
        EventType("single", "x")
    with pytest.raises(EventRegistryError, match="duplicate"):
        registry.register(CORE_EVENTS[0])
    with pytest.raises(EventRegistryError, match="unknown"):
        registry.require("nope.nope")

    class Plugin:
        id = "pay"
        webhook_events = (EventType("pay.invoice.approved", "An invoice was approved"),)

    class Rogue:
        id = "hire"
        webhook_events = (EventType("pay.invoice.paid", "Outside its namespace"),)

    assert "pay.invoice.approved" in build_event_registry([Plugin()])
    with pytest.raises(EventRegistryError, match="namespace"):
        build_event_registry([Rogue()])


def test_envelope_is_canonical_json() -> None:
    event_id = uuid.UUID(int=1)
    org_id = uuid.UUID(int=2)
    payload = envelope(
        event_id=event_id,
        event_type="document.received",
        created_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        org_id=org_id,
        data={"b": 1, "a": "é"},
    )
    assert payload["id"] == f"evt_{event_id.hex}"
    assert payload["created_at"] == "2026-10-07T12:00:00Z"
    raw = serialise(payload)
    assert raw == serialise(json.loads(raw))
    assert b'"data":{"a":"\xc3\xa9","b":1}' in raw


# ------------------------------------------------------------------ sender


class _Stream(httpx.AsyncByteStream):
    """A streamed (not preloaded) response body, like a real network response."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def __aiter__(self) -> Any:
        yield self._data


def _webhook(url: str) -> OutgoingWebhook:
    return OutgoingWebhook(
        delivery_id=uuid.uuid4(), url=url, body=BODY, headers={"Pairly-Signature": "t=1,v1=00"}
    )


async def test_sender_pins_the_checked_address_and_keeps_the_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204, stream=_Stream())

    settings = load_webhook_settings(env=Environment.TEST)
    sender = HttpxSender(
        settings, resolver=_resolver("93.184.215.14"), transport=httpx.MockTransport(handler)
    )
    try:
        result = await sender.send(_webhook("https://hooks.example.com/in"))
    finally:
        await sender.aclose()
    assert result.ok
    assert result.status_code == 204
    (request,) = seen
    assert request.url.host == "93.184.215.14"
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    assert request.content == BODY


async def test_sender_refuses_unsafe_targets_and_never_follows_redirects() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302, headers={"Location": "http://169.254.169.254/"}, stream=_Stream(b"moved")
        )

    settings = load_webhook_settings(env=Environment.TEST)
    transport = httpx.MockTransport(handler)
    private = HttpxSender(settings, resolver=_resolver("10.0.0.7"), transport=transport)
    public = HttpxSender(settings, resolver=_resolver("93.184.215.14"), transport=transport)
    try:
        refused = await private.send(_webhook("https://rebound.example.com/"))
        redirected = await public.send(_webhook("https://hooks.example.com/"))
    finally:
        await private.aclose()
        await public.aclose()
    assert (refused.ok, refused.error) == (False, "unsafe_url")
    assert not redirected.ok
    assert redirected.status_code == 302
    assert calls == 1


async def test_sender_reports_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    settings = load_webhook_settings(env=Environment.TEST)
    for fn, error in ((handler, "ConnectError"), (slow, "timeout")):
        sender = HttpxSender(
            settings, resolver=_resolver("93.184.215.14"), transport=httpx.MockTransport(fn)
        )
        try:
            result = await sender.send(_webhook("https://hooks.example.com/"))
        finally:
            await sender.aclose()
        assert (result.ok, result.error) == (False, error)

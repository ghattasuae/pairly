# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Email message building and transports (SMTP, Resend, fake), Slack transport, tokens."""

from __future__ import annotations

import json
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any, ClassVar

import httpx
import pytest

from pairly_core.integrations.slack.blocks import (
    NATIVE_ACTIONS,
    WEB_ACTIONS,
    approval_buttons,
    message_blocks,
)
from pairly_core.integrations.slack.transport import FakeSlackTransport, HttpSlackTransport
from pairly_core.notifications.email import (
    DeliveryError,
    FakeEmailTransport,
    OutgoingEmail,
    ResendTransport,
    SmtpTransport,
    build_message,
)
from pairly_core.notifications.unsubscribe import (
    MAX_AGE_SECONDS,
    TokenState,
    UnsubscribeClaim,
    inspect_token,
    make_token,
    read_token,
)


def _email(**kw: Any) -> OutgoingEmail:
    values: dict[str, Any] = {
        "to": "person@example.test",
        "sender": "Pairly <no-reply@pairly.localhost>",
        "subject": "Approval needed",
        "text": "Plain text",
        "html": '<!doctype html><html lang="ar" dir="rtl"><body>x</body></html>',
        "locale": "ar",
        "message_id": "abc@pairly.localhost",
        "idempotency_key": "org:key",
    }
    values.update(kw)
    return OutgoingEmail(**values)


def test_message_headers_and_parts() -> None:
    msg = build_message(
        _email(list_unsubscribe="https://api.test/v1/notifications/unsubscribe?token=t")
    )
    assert msg["Message-ID"] == "<abc@pairly.localhost>"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["X-Auto-Response-Suppress"] == "All"
    assert msg["Content-Language"] == "ar"
    assert msg["List-Unsubscribe"] == "<https://api.test/v1/notifications/unsubscribe?token=t>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["Date"]
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [part.get_content_type() for part in msg.iter_parts()]
    assert kinds == ["text/plain", "text/html"]
    html = msg.get_body(preferencelist=("html",))
    assert html is not None
    assert 'dir="rtl"' in html.get_content()


def test_transactional_mail_has_no_unsubscribe_and_replies_thread() -> None:
    msg = build_message(_email(in_reply_to="<orig@vendor.example>", auto_submitted="auto-replied"))
    assert msg["List-Unsubscribe"] is None
    assert msg["In-Reply-To"] == "<orig@vendor.example>"
    assert msg["References"] == "<orig@vendor.example>"
    assert msg["Auto-Submitted"] == "auto-replied"


def test_header_injection_is_neutralised() -> None:
    msg = build_message(_email(subject="Hi\r\nBcc: victim@example.test"))
    assert msg["Bcc"] is None
    assert "\n" not in str(msg["Subject"])
    with pytest.raises(DeliveryError):
        build_message(_email(to="not an address"))


async def test_fake_transport_fails_on_demand() -> None:
    fake = FakeEmailTransport(fail_next=1)
    with pytest.raises(DeliveryError) as exc:
        await fake.send(_email())
    assert exc.value.transient
    assert await fake.send(_email()) == "fake-1"
    assert fake.attempts == 2
    assert len(fake.sent) == 1


async def test_resend_transport() -> None:
    seen: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status["code"] != 200:
            return httpx.Response(status["code"], json={"message": "no"})
        return httpx.Response(200, json={"id": "re_123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resend = ResendTransport(api_key="re_test_key", client=client)
    assert await resend.send(_email(list_unsubscribe="https://api.test/u?token=t")) == "re_123"
    request = seen[0]
    assert request.url == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert request.headers["Idempotency-Key"] == "org:key"
    body = json.loads(request.content)
    assert body["to"] == ["person@example.test"]
    assert body["headers"]["List-Unsubscribe"] == "<https://api.test/u?token=t>"
    assert body["html"].startswith("<!doctype html>")
    for code, transient in ((429, True), (503, True), (422, False)):
        status["code"] = code
        with pytest.raises(DeliveryError) as exc:
            await resend.send(_email())
        assert exc.value.transient is transient
    await client.aclose()


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []
    fail: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.host, self.port = host, port
        self.sent: list[EmailMessage] = []
        self.tls = False
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        return None

    def send_message(self, msg: EmailMessage) -> None:
        if _FakeSMTP.fail is not None:
            raise _FakeSMTP.fail
        self.sent.append(msg)


async def test_smtp_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    smtp = SmtpTransport(host="mailpit", port=1025, require_tls=True)
    provider_id = await smtp.send(_email())
    assert provider_id == "<abc@pairly.localhost>"
    (client,) = _FakeSMTP.instances
    assert client.tls
    assert client.sent[0]["To"] == "person@example.test"
    cases: list[tuple[Exception, bool]] = [
        (smtplib.SMTPRecipientsRefused({}), False),
        (smtplib.SMTPResponseException(550, b"no such user"), False),
        (smtplib.SMTPResponseException(451, b"try later"), True),
        (smtplib.SMTPServerDisconnected("gone"), True),
        (ConnectionRefusedError(), True),
    ]
    for error, transient in cases:
        _FakeSMTP.fail = error
        with pytest.raises(DeliveryError) as exc:
            await smtp.send(_email())
        assert exc.value.transient is transient
    _FakeSMTP.fail = None


async def test_slack_http_transport_refuses_foreign_response_urls() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"})
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    slack = HttpSlackTransport(client=client)
    ts = await slack.post_message(token="xoxb-test", channel="U123", text="hi", blocks=[])  # noqa: S106
    assert ts == "1700000000.000100"
    assert calls[0].headers["Authorization"] == "Bearer xoxb-test"
    await slack.respond("https://hooks.slack.com/actions/T1/1/abc", {"text": "x"})
    for bad in (
        "http://hooks.slack.com/actions/x",
        "https://evil.example/actions",
        "https://hooks.slack.com.evil.example/x",
        "https://hooks.slack.com:8443/x",
        "https://169.254.169.254/latest",
    ):
        with pytest.raises(DeliveryError):
            await slack.respond(bad, {"text": "x"})
    assert len(calls) == 2
    await client.aclose()


async def test_slack_api_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = request.headers.get("x-test-error", "invalid_auth")
        return httpx.Response(200, json={"ok": False, "error": error})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"x-test-error": "ratelimited"}
    )
    slack = HttpSlackTransport(client=client)
    with pytest.raises(DeliveryError) as exc:
        await slack.post_message(token="xoxb-t", channel="U1", text="x", blocks=[])  # noqa: S106
    assert exc.value.transient
    await client.aclose()
    fake = FakeSlackTransport(fail_next=1)
    with pytest.raises(DeliveryError):
        await fake.post_message(token="t", channel="U1", text="x", blocks=[])  # noqa: S106
    assert await fake.post_message(token="t", channel="U1", text="x", blocks=[])  # noqa: S106


def test_approval_buttons_open_the_web_page() -> None:
    buttons, note = approval_buttons(
        locale="en",
        approve_url="https://app.test/approvals/link#A" + "a" * 42,
        approve_token="A" + "a" * 42,
        reject_url="https://app.test/approvals/link#R" + "r" * 42,
        reject_token="R" + "r" * 42,
    )
    blocks = message_blocks("text", buttons, note=note)
    elements = blocks[1]["elements"]
    by_id = {e["action_id"]: e for e in elements}
    assert by_id["approve"]["url"].startswith("https://app.test/approvals/link#")
    assert by_id["reject"]["url"].startswith("https://app.test/approvals/link#")
    assert "url" not in by_id["remind_later"]
    # Every value is a link token, never an approval id.
    for element in elements:
        assert len(element["value"]) == 43
    assert WEB_ACTIONS.isdisjoint(NATIVE_ACTIONS)
    assert "approve" not in NATIVE_ACTIONS
    assert "reject" not in NATIVE_ACTIONS


def test_unsubscribe_tokens() -> None:
    secret = "s" * 40
    claim = UnsubscribeClaim(uuid.uuid4(), uuid.uuid4(), "digest")
    token = make_token(secret, claim)
    assert read_token(secret, token) == claim
    assert read_token("t" * 40, token) is None
    parts = token.split(".")
    forged = ".".join([*parts[:3], "approvals", *parts[4:]])
    assert read_token(secret, forged) is None
    younger = ".".join([*parts[:4], str(int(parts[4]) + 3600), parts[5]])
    assert read_token(secret, younger) is None
    assert read_token(secret, "garbage") is None
    assert read_token(secret, token + "x") is None
    # Expiry: valid up to MAX_AGE_SECONDS, refused after, and refused from the future.
    issued = 1_800_000_000
    old = make_token(secret, claim, now=issued)
    assert read_token(secret, old, now=issued + MAX_AGE_SECONDS) == claim
    assert read_token(secret, old, now=issued + MAX_AGE_SECONDS + 1) is None
    assert read_token(secret, old, now=issued - 3600) is None
    # The landing page tells an authentic expired token apart, and nothing else.
    assert inspect_token(secret, old, now=issued + MAX_AGE_SECONDS + 1) == (
        TokenState.EXPIRED,
        None,
    )
    assert inspect_token(secret, old, now=issued) == (TokenState.VALID, claim)
    assert inspect_token("t" * 40, old, now=issued + MAX_AGE_SECONDS + 1)[0] is (TokenState.INVALID)
    assert inspect_token(secret, forged)[0] is TokenState.INVALID
    assert inspect_token(secret, old, now=issued - 3600)[0] is TokenState.INVALID

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Outgoing email: the RFC 5322 message and the ``EmailTransport`` implementations.

``build_message`` makes a ``multipart/alternative`` message (text and HTML parts) with:

* ``Message-ID`` derived from the notification id, so a retried send carries the same
  id and receivers can drop the duplicate; ``Date``; ``Content-Language``.
* ``Auto-Submitted: auto-generated`` (``auto-replied`` for receipt replies, RFC 3834)
  and ``X-Auto-Response-Suppress: All``, so autoresponders do not answer us (loops).
* ``List-Unsubscribe`` and ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``
  (RFC 2369, RFC 8058) on non-transactional categories only.
* ``In-Reply-To`` and ``References`` for replies.

Header values are single-line: CR and LF are removed before they reach a header, so a
rendered subject can never inject headers.

Transports:

* ``SmtpTransport``: the standard library ``smtplib`` in a worker thread. Implicit TLS
  or STARTTLS with certificate checks when configured. Mailpit locally.
* ``ResendTransport``: Resend's HTTP API through ``httpx``, with an ``Idempotency-Key``
  (the notification's dedupe key) so a retry never sends twice on their side.
* ``FakeEmailTransport``: records messages, can fail on demand. Tests and demos.
* ``OutboxTransport`` (``pairly_core.notifications.outbox``): writes each message to a
  folder. Native development and tests only (no Mailpit).

Errors are ``DeliveryError`` with ``transient`` True (retry later: network, 4xx SMTP,
HTTP 429 and 5xx) or False (give up: rejected recipient, HTTP 4xx).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from typing import Any, Protocol

import httpx

from pairly_core.notifications.settings import NotificationSettings


class DeliveryError(Exception):
    def __init__(self, code: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    sender: str
    subject: str
    text: str
    html: str | None
    locale: str
    message_id: str  # without angle brackets
    idempotency_key: str
    list_unsubscribe: str | None = None
    auto_submitted: str = "auto-generated"
    in_reply_to: str | None = None


def one_line(value: str, limit: int = 998) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:limit]


def _address(value: str) -> str:
    clean = one_line(value, 320)
    _, addr = parseaddr(clean)
    if not addr or "@" not in addr:
        raise DeliveryError("invalid_address", transient=False)
    return clean


def build_message(email: OutgoingEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _address(email.sender)
    msg["To"] = _address(email.to)
    msg["Subject"] = one_line(email.subject)
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = f"<{one_line(email.message_id, 200)}>"
    msg["Auto-Submitted"] = one_line(email.auto_submitted, 32)
    msg["X-Auto-Response-Suppress"] = "All"
    if email.list_unsubscribe:
        msg["List-Unsubscribe"] = f"<{one_line(email.list_unsubscribe, 900)}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if email.in_reply_to:
        ref = f"<{one_line(email.in_reply_to, 900).strip('<>')}>"
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg.set_content(email.text, charset="utf-8")
    if email.html:
        msg.add_alternative(email.html, subtype="html", charset="utf-8")
    # After the body: set_content drops Content-* headers set before it.
    msg["Content-Language"] = one_line(email.locale, 16)
    return msg


class EmailTransport(Protocol):
    name: str

    async def send(self, email: OutgoingEmail) -> str:
        """Send one message. Returns the provider's message id. Raises ``DeliveryError``."""
        ...


# ------------------------------------------------------------------ SMTP


class SmtpTransport:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        secure: bool = False,
        require_tls: bool = False,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._secure = secure
        self._require_tls = require_tls
        self._user = user
        self._password = password
        self._timeout = timeout

    def _send_sync(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if self._secure:
            client = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with client:
            if not self._secure and self._require_tls:
                client.starttls(context=context)
            if self._user and self._password:
                client.login(self._user, self._password)
            client.send_message(msg)

    async def send(self, email: OutgoingEmail) -> str:
        msg = build_message(email)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryError("recipient_refused", transient=False) from None
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError(f"smtp_{exc.smtp_code}", transient=exc.smtp_code < 500) from None
        except (smtplib.SMTPException, OSError):
            raise DeliveryError("smtp_unavailable", transient=True) from None
        return str(msg["Message-ID"])


# ------------------------------------------------------------------ Resend


class ResendTransport:
    name = "resend"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.resend.com",
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, email: OutgoingEmail) -> str:
        msg = build_message(email)
        headers: dict[str, str] = {
            name: str(msg[name])
            for name in (
                "Auto-Submitted",
                "X-Auto-Response-Suppress",
                "List-Unsubscribe",
                "List-Unsubscribe-Post",
                "In-Reply-To",
                "References",
                "Content-Language",
            )
            if msg[name] is not None
        }
        body: dict[str, Any] = {
            "from": str(msg["From"]),
            "to": [str(msg["To"])],
            "subject": str(msg["Subject"]),
            "text": email.text,
            "headers": headers,
        }
        if email.html:
            body["html"] = email.html
        try:
            response = await self._client.post(
                f"{self._base}/emails",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": one_line(email.idempotency_key, 256),
                },
            )
        except httpx.HTTPError:
            raise DeliveryError("resend_unavailable", transient=True) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(f"resend_{response.status_code}", transient=True)
        if response.status_code >= 400:
            raise DeliveryError(f"resend_{response.status_code}", transient=False)
        try:
            provider_id = response.json().get("id")
        except ValueError:
            provider_id = None
        return str(provider_id) if isinstance(provider_id, str) else str(msg["Message-ID"])


# ------------------------------------------------------------------ fake


@dataclass
class FakeEmailTransport:
    name: str = "fake"
    sent: list[OutgoingEmail] = field(default_factory=list)
    messages: list[EmailMessage] = field(default_factory=list)
    attempts: int = 0
    # Fail this many next sends; transient unless ``fail_permanent``.
    fail_next: int = 0
    fail_permanent: bool = False

    async def send(self, email: OutgoingEmail) -> str:
        self.attempts += 1
        msg = build_message(email)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise DeliveryError("fake_failure", transient=not self.fail_permanent)
        self.sent.append(email)
        self.messages.append(msg)
        return f"fake-{len(self.sent)}"


def email_transport_from_settings(
    settings: NotificationSettings, *, client: httpx.AsyncClient | None = None
) -> EmailTransport:
    if settings.email_transport == "fake":
        return FakeEmailTransport()
    if settings.email_transport == "outbox":
        from pathlib import Path

        from pairly_core.notifications.outbox import OutboxTransport

        assert settings.outbox_dir is not None  # noqa: S101 (validated by settings)
        return OutboxTransport(Path(settings.outbox_dir))
    if settings.email_transport == "resend":
        assert settings.resend_api_key is not None  # noqa: S101 (validated by settings)
        return ResendTransport(
            api_key=settings.resend_api_key.get_secret_value(),
            base_url=settings.resend_base_url,
            client=client,
        )
    return SmtpTransport(
        host=settings.smtp_host or "localhost",
        port=settings.smtp_port,
        secure=settings.smtp_secure,
        require_tls=settings.smtp_require_tls,
        user=settings.smtp_user,
        password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
        timeout=settings.smtp_timeout_seconds,
    )

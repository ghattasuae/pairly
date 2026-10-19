# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
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
* Text attachments (``Attachment``), today calendar files: ``text/calendar`` with its
  ``method`` parameter (``REQUEST`` or ``CANCEL``), so mail apps show the invitation.
  The message becomes ``multipart/mixed``. At most ``MAX_ATTACHMENTS`` of at most
  ``MAX_ATTACHMENT_CHARS`` each, with a plain file name.

Header values are single-line: CR and LF are removed before they reach a header, so a
rendered subject can never inject headers.

Transports:

* ``SmtpTransport``: the standard library ``smtplib`` in a worker thread. Implicit TLS
  or STARTTLS with certificate checks when configured. Mailpit locally.
* ``ResendTransport``: Resend's HTTP API (``POST /emails``) through ``httpx``, with an
  ``Idempotency-Key`` (the notification's dedupe key, hashed when longer than Resend's
  256 characters) so a retry within Resend's 24 hour window never sends twice. Reply-To
  and tags (``category``) go in the JSON body. A ``rate_limit_exceeded`` 429 with a short
  ``Retry-After`` is retried once in place (same key, so it cannot duplicate); longer
  waits and quota errors become a transient ``DeliveryError`` carrying ``retry_after``,
  which delivery uses as the earliest next attempt. A 409
  ``concurrent_idempotent_requests`` is transient; ``invalid_idempotent_request`` is not.
* ``FakeEmailTransport``: records messages, can fail on demand. Tests and demos.
* ``OutboxTransport`` (``pairly_core.notifications.outbox``): writes each message to a
  folder. Native development and tests only (no Mailpit).

Errors are ``DeliveryError`` with ``transient`` True (retry later: network, 4xx SMTP,
HTTP 429 and 5xx) or False (give up: rejected recipient, HTTP 4xx). ``retry_after`` is
the provider's requested wait in seconds, when it gave one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from typing import Any, Protocol

import httpx

from pairly_core.notifications.settings import NotificationSettings


class DeliveryError(Exception):
    def __init__(self, code: str, *, transient: bool, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.retry_after = retry_after


MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_CHARS = 256 * 1024
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MIME_RE = re.compile(r"^text/[a-z0-9.+-]{1,40}$")
_PARAM_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
# Resend tag names and values: ASCII letters, digits, underscore and dash, at most 256.
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
MAX_TAGS = 5


@dataclass(frozen=True, slots=True)
class Attachment:
    """A text attachment (a calendar file). ``params`` go on the Content-Type header."""

    filename: str
    mime_type: str
    text: str
    params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _FILENAME_RE.match(self.filename):
            raise ValueError("invalid attachment file name")
        if not _MIME_RE.match(self.mime_type):
            raise ValueError("only text attachments are supported")
        if len(self.text) > MAX_ATTACHMENT_CHARS:
            raise ValueError("attachment too large")
        for key, value in self.params.items():
            if not _PARAM_RE.match(key) or not _PARAM_RE.match(value):
                raise ValueError("invalid attachment parameter")

    def to_json(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "text": self.text,
            "params": dict(self.params),
        }

    @classmethod
    def from_json(cls, data: object) -> Attachment | None:
        """Back from stored content; anything malformed is dropped (None)."""
        if not isinstance(data, dict):
            return None
        params = data.get("params")
        try:
            return cls(
                filename=str(data.get("filename") or ""),
                mime_type=str(data.get("mime_type") or ""),
                text=str(data.get("text") or ""),
                params={str(k): str(v) for k, v in params.items()}
                if isinstance(params, dict)
                else {},
            )
        except ValueError:
            return None


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
    attachments: tuple[Attachment, ...] = ()
    reply_to: str | None = None
    # Provider tags (name, value) for filtering in the provider's dashboard. Pairs that
    # do not match Resend's character rules are dropped, never sent.
    tags: tuple[tuple[str, str], ...] = ()


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
    if email.reply_to:
        msg["Reply-To"] = _address(email.reply_to)
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
    if len(email.attachments) > MAX_ATTACHMENTS:
        raise DeliveryError("too_many_attachments", transient=False)
    for attachment in email.attachments:
        msg.add_attachment(
            attachment.text,
            subtype=attachment.mime_type.split("/", 1)[1],
            charset="utf-8",
            filename=attachment.filename,
            params=dict(attachment.params),
        )
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

RESEND_MAX_IDEMPOTENCY_KEY = 256
# A rate limited send is retried once in place when Resend asks for at most this wait.
RESEND_INLINE_RETRY_SECONDS = 2.0
_RESEND_TRANSIENT_409 = frozenset({"concurrent_idempotent_requests", "resource_locked"})


def resend_idempotency_key(key: str) -> str:
    """The key as sent: one line, 1 to 256 characters (a SHA-256 hex digest if longer)."""
    clean = one_line(key, 10_000)
    if not clean:
        raise DeliveryError("invalid_idempotency_key", transient=False)
    if len(clean) > RESEND_MAX_IDEMPOTENCY_KEY:
        return "sha256-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return clean


def resend_tags(tags: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [
        {"name": name, "value": value}
        for name, value in tags[:MAX_TAGS]
        if _TAG_RE.match(name) and _TAG_RE.match(value)
    ]


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from ``Retry-After`` (or ``ratelimit-reset``); None when absent or odd."""
    for name in ("retry-after", "ratelimit-reset"):
        raw = response.headers.get(name, "").strip()
        if raw.isdigit():
            return float(min(int(raw), 86_400))
    return None


def _error_name(response: httpx.Response) -> str:
    """Resend's error ``name`` (``rate_limit_exceeded``...), only when it looks like one."""
    try:
        data = response.json()
    except ValueError:
        return ""
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,48}", name) else ""


def resend_error(response: httpx.Response) -> DeliveryError:
    """Map a non-2xx Resend answer to a ``DeliveryError`` (code, transient, retry_after).

    See https://resend.com/docs/api-reference/errors. Retried later: 429 (rate limit,
    daily or monthly quota), 5xx, and a 409 for a concurrent request with the same
    idempotency key. Everything else (400 validation, 401 and 403 key problems, 404,
    409 ``invalid_idempotent_request``, 422) is permanent.
    """
    status = response.status_code
    name = _error_name(response)
    code = f"resend_{status}_{name}" if name else f"resend_{status}"
    if status == 429 or status >= 500:
        return DeliveryError(code, transient=True, retry_after=_retry_after(response))
    if status == 409 and name in _RESEND_TRANSIENT_409:
        return DeliveryError(code, transient=True, retry_after=_retry_after(response))
    return DeliveryError(code, transient=False)


class ResendTransport:
    """Resend's ``POST /emails``. Needs an API key with sending access only."""

    name = "resend"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.resend.com",
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        inline_retry_seconds: float = RESEND_INLINE_RETRY_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._inline_retry_seconds = inline_retry_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def body(email: OutgoingEmail, msg: EmailMessage) -> dict[str, Any]:
        """The JSON body for ``POST /emails`` (from the built, header-safe message)."""
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
        if msg["Reply-To"] is not None:
            body["reply_to"] = [str(msg["Reply-To"])]
        if email.html:
            body["html"] = email.html
        tags = resend_tags(email.tags)
        if tags:
            body["tags"] = tags
        if email.attachments:
            body["attachments"] = [
                {
                    "filename": a.filename,
                    "content": base64.b64encode(a.text.encode("utf-8")).decode("ascii"),
                    "content_type": "; ".join(
                        [f"{a.mime_type}; charset=utf-8"]
                        + [f"{k}={v}" for k, v in sorted(a.params.items())]
                    ),
                }
                for a in email.attachments
            ]
        return body

    async def send(self, email: OutgoingEmail) -> str:
        msg = build_message(email)
        body = self.body(email, msg)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": resend_idempotency_key(email.idempotency_key),
        }
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    f"{self._base}/emails", json=body, headers=headers
                )
            except httpx.HTTPError:
                raise DeliveryError("resend_unavailable", transient=True) from None
            if response.status_code < 400:
                break
            error = resend_error(response)
            wait = error.retry_after
            if (
                attempt == 1
                and response.status_code == 429
                and error.code.endswith("rate_limit_exceeded")
                and wait is not None
                and wait <= self._inline_retry_seconds
            ):
                # Same Idempotency-Key: Resend sends at most once.
                await asyncio.sleep(wait)
                continue
            raise error
        try:
            data = response.json()
        except ValueError:
            data = None
        provider_id = data.get("id") if isinstance(data, dict) else None
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

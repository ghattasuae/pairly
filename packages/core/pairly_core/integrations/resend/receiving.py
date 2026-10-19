# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Fetch a received email's raw MIME from Resend's Receiving API.

Resend's ``email.received`` webhook carries metadata only (sender, recipients, subject,
attachment names), never the body or the attachments. The full message is fetched with
the email id:

1. ``GET /emails/receiving/{id}`` returns the email, with ``raw.download_url``: a
   pre-signed, time-limited (about an hour) link to the original ``.eml``.
2. That URL is downloaded with a hard size limit (the intake engine's
   ``max_email_bytes``), https only, redirects followed by hand (https only, at most
   three), so the bytes handed to intake are exactly what the sender's server sent and
   Pairly's own MIME parser (``pairly_core.ingestion.email``) does every check.

The Receiving API needs a Resend API key with full access (a sending-access key is refused
with ``restricted_api_key``). That key is kept apart from the sending key
(``PAIRLY_RESEND_INBOUND_API_KEY``) and only the API process has it.

Errors are ``ReceivingError`` with ``transient`` True (network, 429, 5xx: answer the
webhook non-2xx so Resend retries) or False (bad id, 4xx, too large: answer 2xx and drop).

Sources, read 2026-10-19: https://resend.com/docs/dashboard/receiving/introduction,
https://resend.com/docs/api-reference/emails/retrieve-received-email,
https://resend.com/docs/webhooks/emails/received.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

_UUID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_REDIRECTS: Final = 3


class ReceivingError(Exception):
    def __init__(self, code: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


def valid_email_id(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def _https(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and not parts.username


def _status_error(prefix: str, response: httpx.Response) -> ReceivingError:
    status = response.status_code
    return ReceivingError(f"{prefix}_{status}", transient=status == 429 or status >= 500)


class ResendReceivingClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.resend.com",
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def raw_message(self, email_id: str, *, max_bytes: int) -> bytes:
        """The received email's original bytes. Raises ``ReceivingError``."""
        if not valid_email_id(email_id):
            raise ReceivingError("bad_email_id", transient=False)
        try:
            response = await self._client.get(
                f"{self._base}/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError:
            raise ReceivingError("resend_unavailable", transient=True) from None
        if response.status_code >= 400:
            raise _status_error("resend", response)
        try:
            data = response.json()
        except ValueError:
            raise ReceivingError("resend_bad_response", transient=True) from None
        raw = data.get("raw") if isinstance(data, dict) else None
        url = raw.get("download_url") if isinstance(raw, dict) else None
        if not isinstance(url, str) or not _https(url):
            raise ReceivingError("no_raw_download", transient=False)
        return await self._download(url, max_bytes)

    async def _download(self, url: str, max_bytes: int) -> bytes:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers.get("location", ""))
                        if not _https(target):
                            raise ReceivingError("download_redirect_refused", transient=False)
                        url = target
                        continue
                    if response.status_code >= 400:
                        raise _status_error("download", response)
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise ReceivingError("too_large", transient=False)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ReceivingError("too_large", transient=False)
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError:
                raise ReceivingError("download_unavailable", transient=True) from None
        raise ReceivingError("too_many_redirects", transient=False)

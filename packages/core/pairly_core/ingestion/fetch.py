# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Fetch a document from a URL a client gave us, with SSRF protection (batch API).

The URL is untrusted, exactly like a webhook endpoint, so the same rules apply
(``pairly_core.webhooks.ssrf``): ``https`` only (``http`` only with the development
switch), no credentials or fragment, no local host names, every resolved address must
be public, and the request goes to the exact address that passed the check (``Host``
and TLS SNI keep the name, so the certificate is still verified). On top:

* ``GET`` only, redirects are never followed (a 3xx is a failure), proxy variables are
  ignored, and the whole fetch has one deadline;
* the body streams into a private spool file and stops at the workflow's size limit
  (``Content-Length`` over the limit is refused before reading);
* only a 200 is accepted. The type is still sniffed from the bytes afterwards, by the
  normal ingestion path, never taken from ``Content-Type``;
* the body is never decompressed: the request asks for ``identity`` and a response with
  any other ``Content-Encoding`` is refused before its body is read. httpx would
  otherwise decode gzip, deflate and zstd (installed) a whole network chunk at a time,
  and a few kilobytes of zstd expand to gigabytes in memory before the size limit sees
  a single byte (a decompression bomb against the API process). The raw bytes are
  what is counted and stored.

Failures raise ``FetchError`` with a short reason code (stored on the batch item).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

import httpx

from pairly_core.ingestion.service import SpooledFile, SpoolWriter, TooLargeError
from pairly_core.webhooks.ssrf import (
    Resolver,
    UnsafeWebhookUrlError,
    parse_target,
    resolve_target,
    system_resolver,
)

USER_AGENT: Final = "Pairly-Fetch/1"
DEFAULT_TIMEOUT_SECONDS: Final = 30.0


class FetchError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FetchedFile:
    spooled: SpooledFile
    filename: str | None


def check_url(url: str, *, allow_http: bool = False) -> str:
    """The URL's shape is acceptable (no DNS), or ``UnsafeWebhookUrlError``."""
    parse_target(url, allow_http=allow_http)
    return url


def _filename(url: str) -> str | None:
    last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return last or None


class UrlFetcher:
    """One HTTP client for a batch's URLs. Close it with ``aclose``."""

    def __init__(
        self,
        *,
        allow_http: bool = False,
        allow_private: bool = False,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._allow_http = allow_http
        self._allow_private = allow_private
        self._resolver = resolver
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, directory: Path, *, max_bytes: int) -> FetchedFile:
        try:
            target = parse_target(url, allow_http=self._allow_http)
            ip = await resolve_target(target, self._resolver, allow_private=self._allow_private)
        except UnsafeWebhookUrlError:
            raise FetchError("unsafe_url") from None
        headers = {
            "Host": target.host_header,
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        extensions: dict[str, Any] = {}
        if target.scheme == "https":
            extensions["sni_hostname"] = target.host
        writer: SpoolWriter | None = None
        try:
            async with asyncio.timeout(self._timeout + 1):
                async with self._client.stream(
                    "GET", target.url_for(ip), headers=headers, extensions=extensions
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise FetchError("redirect_not_followed")
                    if response.status_code != 200:
                        raise FetchError(f"http_{response.status_code}")
                    encoding = response.headers.get("content-encoding", "identity")
                    if encoding.strip().lower() not in ("", "identity"):
                        raise FetchError("content_encoding_refused")
                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError("too_large")
                    writer = SpoolWriter(directory, max_bytes=max_bytes)
                    async for chunk in response.aiter_raw():
                        writer.write(chunk)
                    spooled = writer.finish()
                    writer = None
        except TooLargeError:
            raise FetchError("too_large") from None
        except (TimeoutError, httpx.TimeoutException):
            raise FetchError("timeout") from None
        except httpx.HTTPError:
            raise FetchError("network_error") from None
        finally:
            if writer is not None:
                writer.abort()
        return FetchedFile(spooled=spooled, filename=_filename(url))

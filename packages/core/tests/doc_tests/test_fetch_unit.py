# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The batch URL fetcher never decompresses what a client's server sends.

httpx decodes gzip, deflate and (with ``zstandard`` installed) zstd one network chunk at
a time. A few kilobytes of zstd expand to gigabytes in one call, before the size limit
sees a byte, so a decoded read is a decompression bomb against the API process. The
fetcher asks for ``identity``, refuses any other ``Content-Encoding`` before reading the
body, and counts and stores the raw bytes.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import httpx
import pytest

from pairly_core.ingestion.fetch import FetchError, UrlFetcher

PUBLIC_IP = "93.184.216.34"
PDF = b"%PDF-1.7\n" + b"0" * 200 + b"\n%%EOF\n"


async def _resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


def _fetcher(handler: httpx.MockTransport) -> UrlFetcher:
    return UrlFetcher(resolver=_resolver, transport=handler)


async def test_identity_is_requested_and_the_raw_bytes_are_stored(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(PDF))

    fetcher = _fetcher(httpx.MockTransport(remote))
    try:
        fetched = await fetcher.fetch("https://files.example.com/a.pdf", tmp_path, max_bytes=10_000)
    finally:
        await fetcher.aclose()
    assert seen[0].headers["accept-encoding"] == "identity"
    assert fetched.spooled.path.read_bytes() == PDF
    assert fetched.spooled.size == len(PDF)


@pytest.mark.parametrize("encoding", ["gzip", "zstd", "deflate", "br", "gzip, identity"])
async def test_an_encoded_body_is_refused_before_it_is_read(tmp_path: Path, encoding: str) -> None:
    # A gzip bomb: 1 MB of zeros in about a kilobyte (a real one would be gigabytes).
    bomb = gzip.compress(b"\0" * 1_000_000)
    reads: list[int] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def,override]
            reads.append(1)
            yield bomb

    def remote(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=Body())

    fetcher = _fetcher(httpx.MockTransport(remote))
    try:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch("https://files.example.com/a.pdf", tmp_path, max_bytes=500_000)
    finally:
        await fetcher.aclose()
    assert caught.value.reason == "content_encoding_refused"
    assert reads == []  # not a byte of the body was read, so nothing was decoded
    assert list(tmp_path.iterdir()) == []


async def test_the_size_limit_counts_raw_bytes(tmp_path: Path) -> None:
    def remote(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"%PDF-" + b"x" * 2000))

    fetcher = _fetcher(httpx.MockTransport(remote))
    try:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch("https://files.example.com/a.pdf", tmp_path, max_bytes=1000)
    finally:
        await fetcher.aclose()
    assert caught.value.reason == "too_large"

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The clamd INSTREAM client against a socket stub, and the fail-closed reply mapping."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pairly_core.ingestion.scan import (
    EICAR,
    ClamdScanner,
    FakeScanner,
    UnavailableScanner,
    Verdict,
    parse_reply,
)


@dataclass
class Clamd:
    """A tiny clamd stand-in that checks the INSTREAM framing it receives."""

    reply: bytes = b"stream: OK"
    delay: float = 0.0
    received: list[bytes] = field(default_factory=list)
    commands: list[bytes] = field(default_factory=list)
    chunk_sizes: list[int] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        command = (await reader.readuntil(b"\0")).rstrip(b"\0")
        self.commands.append(command)
        if command == b"zVERSION":
            writer.write(b"ClamAV 1.5.4/27800/Wed Oct  7 08:00:00 2026\0")
        elif command == b"zINSTREAM":
            data = b""
            while True:
                (size,) = struct.unpack("!L", await reader.readexactly(4))
                if size == 0:
                    break
                self.chunk_sizes.append(size)
                data += await reader.readexactly(size)
            self.received.append(data)
            await asyncio.sleep(self.delay)
            writer.write(self.reply + b"\0")
        await writer.drain()
        writer.close()


@pytest.fixture
async def clamd() -> AsyncIterator[tuple[Clamd, int]]:
    stub = Clamd()
    server = await asyncio.start_server(stub.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield stub, port


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"synthetic-bytes-" * 1000)
    return path


async def test_instream_framing_and_clean(clamd: tuple[Clamd, int], sample: Path) -> None:
    stub, port = clamd
    scanner = ClamdScanner("127.0.0.1", port, timeout=5, chunk_size=4096)
    result = await scanner.scan_file(sample)
    assert result.verdict is Verdict.CLEAN
    assert result.engine is not None
    assert result.engine.startswith("ClamAV 1.5.4")
    assert stub.received == [sample.read_bytes()]
    assert max(stub.chunk_sizes) == 4096
    assert stub.commands == [b"zVERSION", b"zINSTREAM"]


async def test_infected(clamd: tuple[Clamd, int], sample: Path) -> None:
    stub, port = clamd
    stub.reply = b"stream: Win.Test.EICAR_HDB-1 FOUND"
    result = await ClamdScanner("127.0.0.1", port, timeout=5).scan_file(sample)
    assert result.verdict is Verdict.INFECTED
    assert result.signature == "Win.Test.EICAR_HDB-1"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (b"INSTREAM size limit exceeded. ERROR", "scanner_size_limit"),
        (b"stream: INSTREAM size limit exceeded. ERROR", "scanner_size_limit"),
        (b"stream: Can't allocate memory ERROR", "scanner_error"),
        (b"", "scanner_error"),
    ],
)
async def test_errors_are_never_clean(
    clamd: tuple[Clamd, int], sample: Path, reply: bytes, reason: str
) -> None:
    stub, port = clamd
    stub.reply = reply
    result = await ClamdScanner("127.0.0.1", port, timeout=5).scan_file(sample)
    assert result.verdict is Verdict.ERROR
    assert result.reason == reason


async def test_timeout_fails_closed(clamd: tuple[Clamd, int], sample: Path) -> None:
    stub, port = clamd
    stub.delay = 2.0
    result = await ClamdScanner("127.0.0.1", port, timeout=0.3).scan_file(sample)
    assert result.verdict is Verdict.ERROR
    assert result.reason == "scanner_timeout"


async def test_unreachable_fails_closed(sample: Path) -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    scanner = ClamdScanner("127.0.0.1", port, timeout=1)
    result = await scanner.scan_file(sample)
    assert result.verdict is Verdict.ERROR
    assert result.reason == "scanner_unavailable"
    assert result.engine is None


def test_parse_reply() -> None:
    assert parse_reply(b"stream: OK").verdict is Verdict.CLEAN
    assert parse_reply(b"stream: OK ").verdict is Verdict.CLEAN
    assert parse_reply(b"stream: OKAY").verdict is Verdict.ERROR
    assert parse_reply(b"stream:  FOUND").signature == "unknown"


async def test_fake_and_unavailable_scanners(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.write_bytes(b"synthetic")
    infected = tmp_path / "eicar"
    infected.write_bytes(b"prefix " + EICAR)
    fake = FakeScanner()
    assert (await fake.scan_file(clean)).verdict is Verdict.CLEAN
    assert (await fake.scan_file(infected)).verdict is Verdict.INFECTED
    assert (await FakeScanner(fail="scanner_timeout").scan_file(clean)).reason == "scanner_timeout"
    assert fake.calls == 2
    result = await UnavailableScanner().scan_file(clean)
    assert (result.verdict, result.reason) == (Verdict.ERROR, "scanner_not_configured")

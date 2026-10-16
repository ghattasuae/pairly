# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Misbehaving parsers that prove the sandbox limits hold (tests and ops self-checks).

Use with ``run_sandboxed(..., parser="pairly_core.documents.probes:<name>")``. They
never run on real documents: the registry does not know them.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from pairly_core.documents.model import NormalizedDocument, Page, ParserInfo
from pairly_core.documents.registry import ParseError, ParseOptions


class _Probe:
    name = "probe"
    version = "1"
    media_types: frozenset[str] = frozenset()


class LoopForever(_Probe):
    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        counter = 0
        while True:
            counter += 1


class Allocate(_Probe):
    """Grows memory in 8 MiB steps until something stops it."""

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        hoard: list[bytes] = []
        while True:
            hoard.append(os.urandom(1) * (8 * 1024 * 1024))


class Crash(_Probe):
    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        os.kill(os.getpid(), signal.SIGSEGV)
        raise AssertionError("unreachable")


class Garbage(_Probe):
    """Exits cleanly but writes nothing valid."""

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        os._exit(0)


class Network(_Probe):
    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        import socket

        try:
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        except PermissionError:
            raise ParseError("network_blocked", "network is disabled") from None
        raise ParseError("network_allowed", "network was reachable")


class Echo(_Probe):
    """A well-behaved parser: one page with the file size as text."""

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        size = path.stat().st_size
        secret_seen = any("SECRET" in key or "DATABASE" in key for key in os.environ)
        return NormalizedDocument(
            media_type="application/octet-stream",
            parser=ParserInfo(name="echo", version="1"),
            pages=[Page(number=1, width=1, height=1, unit="pt", text=f"{size} bytes")],
            metadata={"env_has_secrets": str(secret_seen).lower()},
        )


loop = LoopForever()
allocate = Allocate()
crash = Crash()
garbage = Garbage()
network = Network()
echo = Echo()

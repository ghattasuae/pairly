# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Sandbox child: ``python -m pairly_core.documents.sandbox_child --input ... --output ...``.

Order matters: resource limits and the network block are applied before any parser
library is imported, so a malicious document meets the limits from the first byte.
Exit codes: 0 success (output holds a NormalizedDocument), 2 expected parse error
(output holds ``{"code", "message"}``), 3 out of memory, 4 bad arguments, 5 output
larger than allowed (``RLIMIT_FSIZE``; CPython ignores SIGXFSZ, so the write fails with
``EFBIG``). Any other exit or signal is a crash.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import importlib
import json
import os
import resource
import socket
import sys
from pathlib import Path
from typing import Any, NoReturn

EXIT_OK = 0
EXIT_PARSE_ERROR = 2
EXIT_MEMORY = 3
EXIT_USAGE = 4
EXIT_OUTPUT = 5
_ALLOWED_PARSER_PREFIX = "pairly_core.documents."


def _limit(name: str, value: int, *, slack: int = 0) -> None:
    """Lower both the soft and the hard limit, so the parser cannot raise them again.

    ``slack`` puts the hard limit a little above the soft one (CPU: SIGXCPU first).
    """
    kind = getattr(resource, name, None)
    if kind is None:
        return
    _soft, hard = resource.getrlimit(kind)
    new_hard = value + slack
    if hard != resource.RLIM_INFINITY:
        new_hard = min(new_hard, hard)
        value = min(value, new_hard)
    # Not every kernel enforces every limit (macOS has no RLIMIT_AS); the parent's
    # watchdog covers what the kernel does not.
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(kind, (value, new_hard))


def apply_limits(*, cpu_seconds: int, memory_bytes: int, max_output_bytes: int) -> None:
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_CPU", cpu_seconds, slack=2)
    _limit("RLIMIT_AS", memory_bytes)
    _limit("RLIMIT_FSIZE", max_output_bytes)
    _limit("RLIMIT_NOFILE", 256)
    os.umask(0o077)


def block_network() -> None:
    """Make Python-level network use fail. Parsers never need the network."""

    def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise PermissionError("network access is disabled in the parser sandbox")

    class _NoSocket(socket.socket):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _refuse()

    socket.socket = _NoSocket  # type: ignore[misc]
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse


def _write(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)


def _load_parser(ref: str) -> Any:
    module_name, _, attr = ref.partition(":")
    if not module_name.startswith(_ALLOWED_PARSER_PREFIX) or not attr:
        raise SystemExit(EXIT_USAGE)
    return getattr(importlib.import_module(module_name), attr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pairly-sandbox-child")
    ap.add_argument("--input", required=True)
    ap.add_argument("--media-type", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--options", required=True)
    ap.add_argument("--cpu-seconds", type=int, required=True)
    ap.add_argument("--memory-bytes", type=int, required=True)
    ap.add_argument("--max-output-bytes", type=int, required=True)
    ap.add_argument("--parser")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE

    apply_limits(
        cpu_seconds=args.cpu_seconds,
        memory_bytes=args.memory_bytes,
        max_output_bytes=args.max_output_bytes,
    )
    block_network()

    # Parser libraries are imported only now, under the limits.
    from pairly_core.documents.registry import ParseError, ParseOptions, default_registry

    output = Path(args.output)
    try:
        options = ParseOptions.model_validate_json(args.options)
        if args.parser:
            parser = _load_parser(args.parser)
        else:
            parser = default_registry().get(args.media_type)
        document = parser.parse(Path(args.input), options)
        document = document.model_copy(update={"media_type": args.media_type})
        _write(output, document.model_dump_json(exclude_defaults=False))
    except ParseError as exc:
        _write(output, json.dumps({"code": exc.code, "message": exc.message[:500]}))
        return EXIT_PARSE_ERROR
    except MemoryError:
        return EXIT_MEMORY
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            return EXIT_OUTPUT
        raise
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

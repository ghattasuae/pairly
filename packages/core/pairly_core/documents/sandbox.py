# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Run a parser in a separate, resource-limited subprocess.

Untrusted documents are parsed by ``python -m pairly_core.documents.sandbox_child`` in
its own process group, with:

* CPU time: ``RLIMIT_CPU`` (the kernel sends SIGXCPU, then SIGKILL).
* Memory: ``RLIMIT_AS`` where the kernel enforces it (Linux, the production target).
  The parent also samples the child's resident memory and kills it above the limit,
  which covers macOS (no ``RLIMIT_AS``) and memory held by helper processes is capped by
  their own inherited ``RLIMIT_AS``.
* Wall clock: the parent kills the whole process group when it runs too long.
* Output: ``RLIMIT_FSIZE`` caps files the child writes, and the parent refuses an
  output file above ``max_output_bytes``.
* No core dumps, a private temp directory as ``HOME``, ``TMPDIR`` and working
  directory, and an environment built from scratch: no database URLs, storage keys or
  any other secret reaches the child. The child also disables Python sockets before
  importing any parser library. Parsers need no network.
  TODO(infra): run parsing in a dedicated container with ``network_mode: none`` for
  kernel-level network isolation.

The input is passed by path, the output comes back as a JSON file. Anything other than
a clean exit with a valid ``NormalizedDocument`` is a failure with a reason (fail
closed): ``timeout``, ``cpu_limit``, ``memory_limit``, ``output_limit``, ``crashed``,
``invalid_output`` or the parser's own ``ParseError`` code.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog
from pydantic import ValidationError

from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.registry import ParseOptions

log = structlog.get_logger(__name__)

CHILD_MODULE: Final = "pairly_core.documents.sandbox_child"
# Exit codes of the child (see sandbox_child).
EXIT_OK: Final = 0
EXIT_PARSE_ERROR: Final = 2
EXIT_MEMORY: Final = 3
EXIT_USAGE: Final = 4
EXIT_OUTPUT: Final = 5
_POLL_SECONDS = 0.05
# Environment variables the child may inherit (none of them secret).
_ENV_PASSTHROUGH = ("PATH", "TESSDATA_PREFIX", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    wall_seconds: float = 120.0
    cpu_seconds: int = 90
    memory_bytes: int = 1536 * 1024 * 1024
    max_output_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SandboxResult:
    document: NormalizedDocument | None
    error: str | None = None
    detail: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.document is not None and self.error is None


# ------------------------------------------------------------------ memory sampling


class _RusageInfoV0(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


_libproc: ctypes.CDLL | None = None


def _rss_darwin(pid: int) -> int | None:
    global _libproc
    if _libproc is None:
        name = ctypes.util.find_library("proc")
        if name is None:
            return None
        _libproc = ctypes.CDLL(name, use_errno=True)
    info = _RusageInfoV0()
    if _libproc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return None
    return int(max(info.ri_phys_footprint, info.ri_resident_size))


def _rss_linux(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")


def process_memory(pid: int) -> int | None:
    """Resident memory of ``pid`` in bytes, or None when it cannot be read."""
    if sys.platform == "darwin":
        return _rss_darwin(pid)
    if sys.platform.startswith("linux"):
        return _rss_linux(pid)
    return None  # pragma: no cover


# ------------------------------------------------------------------ runner


def _child_env(workdir: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if os.environ.get(k)}
    env.update(
        {
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            # Tesseract and BLAS: one thread, less heat, predictable CPU accounting.
            "OMP_THREAD_LIMIT": "1",
            "OMP_NUM_THREADS": "1",
        }
    )
    return env


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def run_sandboxed(
    input_path: Path,
    media_type: str,
    *,
    workdir: Path,
    options: ParseOptions | None = None,
    limits: SandboxLimits | None = None,
    parser: str | None = None,
) -> SandboxResult:
    """Parse ``input_path`` (already inside ``workdir``) in a limited subprocess.

    ``parser`` (``module:attribute`` under ``pairly_core.documents``) replaces the
    registry lookup; it exists for the sandbox self-tests (see ``probes``).
    """
    options = options or ParseOptions()
    limits = limits or SandboxLimits()
    output = workdir / "result.json"
    stderr_path = workdir / "stderr.log"
    output.unlink(missing_ok=True)
    args = [
        sys.executable,
        "-m",
        CHILD_MODULE,
        "--input",
        str(input_path),
        "--media-type",
        media_type,
        "--output",
        str(output),
        "--options",
        options.model_dump_json(),
        "--cpu-seconds",
        str(limits.cpu_seconds),
        "--memory-bytes",
        str(limits.memory_bytes),
        "--max-output-bytes",
        str(limits.max_output_bytes),
    ]
    if parser is not None:
        args += ["--parser", parser]
    started = time.monotonic()
    with stderr_path.open("wb") as stderr:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr,
            cwd=workdir,
            env=_child_env(workdir),
            start_new_session=True,
        )
        killed: str | None = None
        try:
            while True:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_POLL_SECONDS)
                    break
                except TimeoutError:
                    pass
                if time.monotonic() - started > limits.wall_seconds:
                    killed = "timeout"
                    _kill_group(proc)
                    await proc.wait()
                    break
                rss = process_memory(proc.pid)
                if rss is not None and rss > limits.memory_bytes:
                    killed = "memory_limit"
                    _kill_group(proc)
                    await proc.wait()
                    break
        except BaseException:
            _kill_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        finally:
            # Helpers (tesseract) left in the group never outlive the parse.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
    elapsed = round(time.monotonic() - started, 3)
    code = proc.returncode
    if killed is not None:
        return _failed(killed, f"parser killed: {killed}", elapsed)
    if code == EXIT_OK:
        return _read_output(output, limits, elapsed)
    if code == EXIT_PARSE_ERROR:
        return _read_error(output, elapsed)
    if code == EXIT_MEMORY:
        return _failed("memory_limit", "parser ran out of memory", elapsed)
    if code == EXIT_OUTPUT:
        return _failed("output_limit", "parser output too large", elapsed)
    if code is not None and code < 0:
        sig = -code
        if sig == signal.SIGXCPU or (sig == signal.SIGKILL and _cpu_exhausted(limits, elapsed)):
            return _failed("cpu_limit", "parser used its CPU time", elapsed)
        if sig == signal.SIGXFSZ:
            return _failed("output_limit", "parser output too large", elapsed)
        return _failed("crashed", f"parser died with signal {sig}", elapsed)
    return _failed("crashed", f"parser exited with code {code}", elapsed)


def _cpu_exhausted(limits: SandboxLimits, elapsed: float) -> bool:
    return elapsed >= limits.cpu_seconds


def _failed(error: str, detail: str, elapsed: float) -> SandboxResult:
    log.warning("sandbox_parse_failed", error=error, elapsed=elapsed)
    return SandboxResult(document=None, error=error, detail=detail, elapsed=elapsed)


def _read_output(output: Path, limits: SandboxLimits, elapsed: float) -> SandboxResult:
    try:
        size = output.stat().st_size
    except OSError:
        return _failed("invalid_output", "parser wrote no output", elapsed)
    if size > limits.max_output_bytes:
        return _failed("output_limit", "parser output too large", elapsed)
    try:
        document = NormalizedDocument.model_validate_json(output.read_bytes())
    except (ValidationError, ValueError):
        return _failed("invalid_output", "parser output is not a NormalizedDocument", elapsed)
    return SandboxResult(document=document, elapsed=elapsed)


def _read_error(output: Path, elapsed: float) -> SandboxResult:
    try:
        data = json.loads(output.read_bytes()[:65536])
        code = str(data.get("code", "parse_error"))[:64]
        detail = str(data.get("message", ""))[:500]
    except (OSError, ValueError, AttributeError):
        code, detail = "parse_error", "parser failed"
    if not code.replace("_", "").isalnum():
        code = "parse_error"
    return _failed(code, detail, elapsed)

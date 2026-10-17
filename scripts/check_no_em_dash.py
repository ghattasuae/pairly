#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Fail if any tracked docs or UI text file contains an em dash (U+2014).

Pairly copy style forbids em dashes in docs, README files, Markdown and web UI text.
Scope: every git-tracked file under ``docs/`` or ``apps/web/``, every ``README*`` file,
and every ``*.md`` file. ``LICENSE`` and ``BRIEF.md`` are excluded.

Usage:
    check_no_em_dash.py            # check all tracked files in scope
    check_no_em_dash.py FILE ...   # check only the given files that are in scope
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

EM_DASH = "\u2014"
EM_DASH_BYTES = EM_DASH.encode("utf-8")
EXCLUDED = frozenset({"LICENSE", "BRIEF.md"})


def in_scope(path: str) -> bool:
    p = PurePosixPath(path)
    if path in EXCLUDED:
        return False
    return (
        path.startswith(("docs/", "apps/web/"))
        or p.name.startswith("README")
        or p.suffix.lower() == ".md"
    )


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 (git from PATH is intended)
        check=True,
        capture_output=True,
    ).stdout
    return [f for f in out.decode("utf-8").split("\0") if f]


def find_em_dashes(path: str) -> list[tuple[int, int]]:
    """Return (line, column) of every em dash in a text file. Binary files are skipped."""
    try:
        data = Path(path).read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return []
    if b"\0" in data[:8192] or EM_DASH_BYTES not in data:
        return []
    hits: list[tuple[int, int]] = []
    text = data.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        col = line.find(EM_DASH)
        while col != -1:
            hits.append((lineno, col + 1))
            col = line.find(EM_DASH, col + 1)
    return hits


def main(argv: list[str]) -> int:
    candidates = [a.replace("\\", "/") for a in argv] if argv else tracked_files()
    failed = False
    for path in sorted(set(candidates)):
        if not in_scope(path):
            continue
        for line, col in find_em_dashes(path):
            failed = True
            print(f"{path}:{line}:{col}: em dash (U+2014) found, use a comma, colon or full stop")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

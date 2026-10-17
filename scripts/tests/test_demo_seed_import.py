# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The demo seed imports on its own, both as ``scripts.demo_seed`` and as a plain script.

Each check runs in a fresh interpreter, so a module an earlier test left in
``sys.modules`` (``test_demo_pay.py`` registers ``demo_pay``) cannot hide a broken
import. Neither check needs a database: importing the seed connects to nothing, and the
script exits early without ``PAIRLY_DEVSEED=1``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "demo_seed.py"


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PAIRLY_DEVSEED"}}


def test_the_seed_imports_as_a_module_of_the_scripts_directory() -> None:
    # The API tests import it this way, with only the repository root on the path.
    result = subprocess.run(
        [sys.executable, "-c", "import importlib; importlib.import_module('scripts.demo_seed')"],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_seed_runs_as_a_plain_script() -> None:
    # make demo runs it this way, with only scripts/ on the path; without
    # PAIRLY_DEVSEED=1 it refuses and exits 2 once every import has succeeded.
    result = subprocess.run(  # noqa: S603 (fixed argv: this interpreter and the seed)
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "PAIRLY_DEVSEED=1" in result.stderr

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tests for scripts/third_party_notices.py (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "third_party_notices", ROOT / "scripts" / "third_party_notices.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["third_party_notices"] = module
    spec.loader.exec_module(module)
    return module


notices = _load()


def test_bundled_components_are_listed() -> None:
    text = notices.render([], [], notices.bundled_components())
    assert "## Bundled components (2)" in text
    assert "| PDFium |" in text
    assert "pypdfium2" in text
    assert "DejaVu Sans" in text
    assert "LICENSE_DEJAVU" in text


def test_lock_only_packages_are_listed_and_filled(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "tzdata"\nversion = "2026.5"\n\n'
        '[[package]]\nname = "Jinja2"\nversion = "3.1.6"\n\n'
        '[[package]]\nname = "pairly-core"\nversion = "0.0.0"\n'
    )
    installed = [notices.Component("jinja2", "3.1.6", "BSD-3-Clause")]
    merged = notices.with_locked_python(installed, tmp_path)
    assert [(c.name, c.license) for c in merged] == [
        ("jinja2", "BSD-3-Clause"),
        ("tzdata", "UNKNOWN (not installed here)"),
    ]
    filled = notices.fill_from_registry(merged, lambda name, version: "Apache-2.0")
    assert filled[1].license == "Apache-2.0 (from the registry)"
    # Installed packages are never looked up again.
    assert filled[0].license == "BSD-3-Clause"


def test_notice_file_has_no_unknown_license() -> None:
    text = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| ")]
    assert rows
    assert not [row for row in rows if "UNKNOWN" in row]
    for name in ("pypdfium2", "PDFium", "DejaVu Sans", "Jinja2", "MarkupSafe", "reportlab"):
        assert f"| {name}" in text, name
    assert "| arabic-reshaper |" in text

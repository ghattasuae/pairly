# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tests for scripts/third_party_notices.py (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

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
    text = notices.render([], [], notices.bundled_components(ROOT))
    assert "## Bundled components (12)" in text
    # The web app's self-hosted fonts (apps/web/src/fonts), each with its OFL text.
    for family in ("IBM Plex Sans (", "IBM Plex Sans Arabic (", "IBM Plex Mono (", "Fraunces ("):
        assert f"| {family}" in text
    assert "OFL-1.1 (SIL Open Font License 1.1" in text
    assert "| PDFium |" in text
    assert "pypdfium2" in text
    assert "DejaVu Sans" in text
    assert "(packages/core/pairly_core/decisions/fonts/LICENSE_DEJAVU)" in text
    assert "| disposable-email-domains (subset) | curated 2026-10 | CC0-1.0 |" in text
    assert "| Redoc (standalone bundle) | 2.5.4 | MIT;" in text
    assert "| React (inside the Redoc bundle) |" in text
    assert "| DOMPurify (inside the Redoc bundle) |" in text
    assert "| pdf.js (pdfjs-dist worker and standard fonts) |" in text
    assert "(LICENSES/third-party/pdfjs/LICENSE_LIBERATION.txt)" in text
    assert "(LICENSES/third-party/pdfium/pdfium.txt)" in text


def test_the_repository_passes_validation() -> None:
    bundled = notices.bundled_components(ROOT)
    text = notices.render([], [], bundled)
    assert notices.validate(ROOT, bundled, text) == []


def test_every_linked_license_text_exists() -> None:
    for entry in notices.bundled_components(ROOT):
        assert entry.texts, entry.name
        for _, target in entry.texts:
            if not target.startswith("https://"):
                assert (ROOT / target).is_file(), target


def _fake_repo(tmp_path: Path) -> Path:
    font_dir = tmp_path / notices.FONT_DIR
    font_dir.mkdir(parents=True)
    (font_dir / "DejaVuSans.ttf").write_bytes(b"\0font")
    (font_dir / "LICENSE_DEJAVU").write_text("Bitstream Vera\n")
    return tmp_path


def test_a_known_asset_missing_from_the_output_fails(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bundled = [b for b in notices.bundled_components(root) if b.key != "dejavu"]
    problems = notices.validate(root, bundled, notices.render([], [], bundled))
    assert any("'dejavu'" in p for p in problems)


def test_a_missing_license_text_fails(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    (root / notices.FONT_DIR / "LICENSE_DEJAVU").unlink()
    bundled = [b for b in notices.bundled_components(root) if b.key == "dejavu"]
    problems = notices.validate(root, bundled, notices.render([], [], bundled))
    assert any("LICENSE_DEJAVU" in p for p in problems)


def test_an_unlisted_asset_fails(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    stray = root / "apps" / "web" / "public" / "Inter.woff2"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"\0")
    tess = root / "infra" / "tessdata" / "ara.traineddata"
    tess.parent.mkdir(parents=True)
    tess.write_bytes(b"\0")
    bundled = [b for b in notices.bundled_components(root) if b.key == "dejavu"]
    problems = notices.validate(root, bundled, notices.render([], [], bundled))
    assert any("Inter.woff2" in p for p in problems)
    assert any("ara.traineddata" in p for p in problems)
    assert not any("DejaVuSans.ttf" in p for p in problems)


def test_main_writes_nothing_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path)
    (root / notices.FONT_DIR / "LICENSE_DEJAVU").unlink()
    out = tmp_path / "NOTICE.md"
    monkeypatch.setattr(notices, "python_components", list)
    assert notices.main(["--root", str(root), "-o", str(out)]) == 1
    assert not out.exists()


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

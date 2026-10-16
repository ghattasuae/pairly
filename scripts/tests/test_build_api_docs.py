# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``make api-docs``: the page is self-contained and fetches nothing at run time, the
spec round-trips, and the vendored Redoc bundle is the reviewed one (license, checksum),
checked by the build itself too (security review 2026-10, L-11)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "packages" / "shared-ts" / "openapi" / "v1.json"
VENDOR = ROOT / "infra" / "api-docs" / "vendor" / "redoc"
REDOC_SHA256 = "dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_api_docs", ROOT / "scripts" / "build_api_docs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_api_docs"] = module
    spec.loader.exec_module(module)
    return module


docs = _load()


def test_the_vendored_redoc_is_the_reviewed_mit_build() -> None:
    bundle = (VENDOR / "redoc.standalone.js").read_bytes()
    assert hashlib.sha256(bundle).hexdigest() == REDOC_SHA256
    license_text = (VENDOR / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("The MIT License (MIT)")
    third_party = (VENDOR / "redoc.standalone.js.LICENSE.txt").read_text(encoding="utf-8")
    for strong in ("GNU General Public", "GPL-", "AGPL", "SSPL", "Business Source"):
        assert strong not in third_party
    assert REDOC_SHA256 in (VENDOR / "README.md").read_text(encoding="utf-8")


def test_the_page_is_self_contained_and_offline(tmp_path: Path) -> None:
    spec_file, page = docs.build(tmp_path, SNAPSHOT)
    assert json.loads(spec_file.read_text(encoding="utf-8")) == json.loads(
        SNAPSHOT.read_text(encoding="utf-8")
    )
    text = page.read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text)
    assert csp is not None
    directives = [p.strip().split(" ", 1) for p in csp.group(1).split(";")]
    policy = {d[0]: d[1] if len(d) > 1 else "" for d in directives}
    assert policy["default-src"] == "'none'"
    assert policy["connect-src"] == "'none'"
    assert policy["img-src"] == "data:"
    assert "http" not in csp.group(1)
    # Nothing is loaded from elsewhere: no external script, style sheet or frame.
    assert not re.search(r'<(script|link|iframe|img)[^>]+(src|href)="(https?:)?//', text)
    assert "<link" not in text
    # The bundle is embedded byte for byte, and the spec parses back.
    embedded = re.search(r'<script src="data:text/javascript;base64,([A-Za-z0-9+/=]+)">', text)
    assert embedded is not None
    assert hashlib.sha256(base64.b64decode(embedded.group(1))).hexdigest() == REDOC_SHA256
    inline = re.search(r'<script type="application/json" id="openapi">(.*?)</script>', text, re.S)
    assert inline is not None
    assert "<" not in inline.group(1)
    assert json.loads(inline.group(1))["openapi"].startswith("3.")
    assert (tmp_path / "redoc-LICENSE.txt").exists()


def test_a_hostile_description_cannot_break_out_of_the_page(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "x</title><script>alert(1)</script>", "version": "1"},
                "paths": {},
                "components": {"schemas": {"X": {"description": "</script><!--<script>"}}},
            }
        ),
        encoding="utf-8",
    )
    _, page = docs.build(tmp_path / "out", spec)
    text = page.read_text(encoding="utf-8")
    assert "<script>alert(1)" not in text
    assert text.count("</script>") == 3  # the JSON data, the bundle and the start-up code


def test_the_build_checks_the_recorded_checksum() -> None:
    assert docs.REDOC_SHA256 == REDOC_SHA256


def test_a_changed_bundle_stops_the_build_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = tmp_path / "vendor"
    shutil.copytree(VENDOR, vendor)
    bundle = vendor / "redoc.standalone.js"
    bundle.write_bytes(bundle.read_bytes() + b"\n;fetch('https://example.invalid/x');\n")
    monkeypatch.setattr(docs, "VENDOR", vendor)
    out = tmp_path / "out"
    with pytest.raises(docs.BundleChecksumError) as refused:
        docs.build(out, SNAPSHOT)
    assert REDOC_SHA256 in str(refused.value)
    assert not out.exists()
    # Through the command line too (what `make api-docs` runs): a non-zero exit.
    with pytest.raises(SystemExit) as exited:
        docs.main(["--out", str(out), "--spec", str(SNAPSHOT)])
    assert exited.value.code not in (0, None)
    assert not out.exists()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Unit tests for the license gate classifier (scripts/check_licenses.py)."""

from __future__ import annotations

import importlib.util
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_licenses.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licenses", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_licenses"] = module
    spec.loader.exec_module(module)
    return module


cl = _load()
V = cl.Verdict


@pytest.mark.parametrize(
    "text",
    [
        "MIT",
        "mit",
        "MIT License",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "3-Clause BSD License",
        "BSD 2-Clause License",
        "BSD",
        "Apache-2.0",
        "Apache Software License",
        "ISC",
        "MPL-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "PSF-2.0",
        "Python-2.0",
        "0BSD",
        "Unlicense",
        "CC0-1.0",
        "BlueOak-1.0.0",
        "Zlib",
        "MIT-0",
    ],
)
def test_allowed(text: str) -> None:
    assert cl.classify_expression(text) is V.ALLOW


@pytest.mark.parametrize(
    "text",
    [
        "LGPL-2.1",
        "LGPL-3.0-or-later",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "EPL-2.0",
    ],
)
def test_review(text: str) -> None:
    assert cl.classify_expression(text) is V.REVIEW


@pytest.mark.parametrize(
    "text",
    [
        "GPL-2.0",
        "GPL-3.0-or-later",
        "AGPL-3.0",
        "AGPL-3.0-only",
        "SSPL-1.0",
        "BUSL-1.1",
        "GNU General Public License v3 (GPLv3)",
        "MIT AND Commons-Clause",
        "Apache-2.0 WITH Commons Clause",
        "GPL-2.0 WITH Classpath-exception-2.0",
    ],
)
def test_denied(text: str) -> None:
    assert cl.classify_expression(text) is V.DENY


@pytest.mark.parametrize(
    "text", ["", "   ", "Proprietary", "SEE LICENSE IN LICENSE.txt", "CC-BY-4.0", "WTFPL", "(MIT"]
)
def test_unknown(text: str) -> None:
    assert cl.classify_expression(text) is V.UNKNOWN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MIT OR Apache-2.0", V.ALLOW),
        ("(MIT OR GPL-3.0)", V.ALLOW),
        ("GPL-3.0 OR MIT", V.ALLOW),
        ("MIT AND GPL-3.0", V.DENY),
        ("MIT AND PSF-2.0", V.ALLOW),
        ("(MIT AND Zlib) OR GPL-2.0", V.ALLOW),
        ("MIT AND (LGPL-2.1 OR GPL-2.0)", V.REVIEW),
        ("Apache-2.0 WITH LLVM-exception", V.ALLOW),
        ("(MIT/Apache-2.0)", V.ALLOW),
        ("MIT AND Unknown-Thing", V.UNKNOWN),
        ("MIT or Apache-2.0", V.ALLOW),
    ],
)
def test_expressions(text: str, expected: object) -> None:
    assert cl.classify_expression(text) is expected


def _meta(**fields: str | list[str]) -> Message:
    msg = Message()
    for key, value in fields.items():
        header = key.replace("_", "-")
        for item in value if isinstance(value, list) else [value]:
            msg[header] = item
    return msg


def test_python_license_prefers_expression() -> None:
    meta = _meta(
        License_Expression="Apache-2.0",
        License="GPL",
        Classifier=["License :: OSI Approved :: GNU General Public License (GPL)"],
    )
    assert cl.python_license(meta) == "Apache-2.0"


def test_python_license_falls_back_to_classifiers_for_full_text() -> None:
    meta = _meta(
        License="Copyright (c) someone\nPermission is hereby granted...",
        Classifier=[
            "License :: OSI Approved",
            "License :: OSI Approved :: MIT License",
            "License :: OSI Approved :: Apache Software License",
        ],
    )
    text = cl.python_license(meta)
    assert cl.classify_expression(text) is V.ALLOW
    assert set(text.split(" OR ")) == {"MIT", "Apache-2.0"}


def test_python_license_unrecognised_classifier_with_known_one() -> None:
    meta = _meta(
        Classifier=[
            "License :: OSI Approved :: Historical Permission Notice and Disclaimer (HPND)",
            "License :: OSI Approved :: MIT License",
        ]
    )
    assert cl.classify_expression(cl.python_license(meta)) is V.ALLOW


def test_python_license_missing() -> None:
    assert cl.python_license(_meta(Name="x")) == ""


class _Dist:
    def __init__(self, meta: Message) -> None:
        self.metadata = meta


def test_python_packages_skips_own_and_normalises_names() -> None:
    dists = [
        _Dist(_meta(Name="pairly-core", Version="0.0.0", License_Expression="AGPL-3.0-only")),
        _Dist(_meta(Name="pairly", Version="0.0.0", License_Expression="AGPL-3.0-only")),
        _Dist(_meta(Name="Some_Lib", Version="1.2", License_Expression="MIT")),
    ]
    packages = cl.python_packages(dists)
    assert [(p.name, p.license) for p in packages] == [("some-lib", "MIT")]


def test_npm_packages_flattens_and_skips_own() -> None:
    data = {
        "MIT": [
            {"name": "left-pad", "versions": ["1.0.0", "1.1.0"], "license": "MIT"},
            {"name": "@pairly/web", "versions": ["0.0.0"], "license": "AGPL-3.0-only"},
        ],
        "Unknown": [{"name": "mystery", "versions": ["2.0.0"], "license": "Unknown"}],
        "(MIT OR CC0-1.0)": [{"name": "dual", "versions": ["3.0.0"]}],
    }
    packages = cl.npm_packages(data)
    assert [(p.name, p.version, p.license) for p in packages] == [
        ("dual", "3.0.0", "(MIT OR CC0-1.0)"),
        ("left-pad", "1.0.0", "MIT"),
        ("left-pad", "1.1.0", "MIT"),
        ("mystery", "2.0.0", ""),
    ]
    assert cl.classify_expression("") is V.UNKNOWN


def _write_exceptions(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "exceptions.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_evaluate_fails_on_deny_and_unknown() -> None:
    packages = [
        cl.Package("npm", "ok", "1", "MIT"),
        cl.Package("npm", "lgpl", "1", "LGPL-3.0"),
        cl.Package("python", "gpl", "1", "GPL-3.0"),
        cl.Package("python", "nolicense", "1", ""),
    ]
    report = cl.evaluate(packages, [])
    assert not report.ok
    assert [p.name for p in report.allowed] == ["ok"]
    assert [p.name for p in report.review] == ["lgpl"]
    assert {(p.name, v) for p, v in report.failures} == {
        ("gpl", V.DENY),
        ("nolicense", V.UNKNOWN),
    }


def test_exception_needs_exact_license_match(tmp_path: Path) -> None:
    path = _write_exceptions(
        tmp_path,
        "exceptions:\n"
        "  - ecosystem: python\n"
        "    name: Some_GPL\n"
        "    license: GPL-3.0\n"
        "    reason: Build-time tool only, never shipped or linked into Pairly.\n"
        "  - ecosystem: npm\n"
        "    name: gone\n"
        "    license: WTFPL\n"
        "    reason: Kept to prove that unused exceptions are reported.\n",
    )
    exceptions = cl.load_exceptions(path)
    ok = cl.evaluate([cl.Package("python", "some-gpl", "1", "GPL-3.0")], exceptions)
    assert ok.ok
    assert [e.name for e in ok.unused_exceptions] == ["gone"]

    changed = cl.evaluate([cl.Package("python", "some-gpl", "2", "AGPL-3.0")], exceptions)
    assert not changed.ok


@pytest.mark.parametrize(
    "body",
    [
        "exceptions:\n  - ecosystem: npm\n    name: x\n    license: GPL-3.0\n",
        "exceptions:\n  - ecosystem: npm\n    name: x\n    license: GPL-3.0\n    reason: ok\n",
        "exceptions:\n  - ecosystem: rubygems\n    name: x\n    license: GPL-3.0\n"
        "    reason: a long enough reason for the check\n",
        "exceptions: {}\n",
    ],
)
def test_bad_exception_files_are_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(SystemExit):
        cl.load_exceptions(_write_exceptions(tmp_path, body))


def test_main_with_pnpm_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pnpm = tmp_path / "pnpm.json"
    pnpm.write_text('{"GPL-3.0": [{"name": "bad", "versions": ["1.0.0"]}]}', encoding="utf-8")
    empty = _write_exceptions(tmp_path, "exceptions: []\n")
    code = cl.main(["--only", "npm", "--pnpm-json", str(pnpm), "--exceptions", str(empty)])
    assert code == 1
    assert "denied: npm bad 1.0.0: GPL-3.0" in capsys.readouterr().out


def test_shipped_exceptions_file_is_valid() -> None:
    cl.load_exceptions(cl.DEFAULT_EXCEPTIONS)


def test_review_packages_can_be_recorded_and_npm_names_can_use_a_prefix(tmp_path: Path) -> None:
    path = _write_exceptions(
        tmp_path,
        "exceptions:\n"
        "  - ecosystem: python\n"
        "    name: psycopg\n"
        "    license: LGPL-3.0-only\n"
        "    reason: Unmodified, dynamically imported library that a user can replace.\n"
        "  - ecosystem: npm\n"
        '    name: "@img/sharp-libvips-*"\n'
        "    license: LGPL-3.0-or-later\n"
        "    reason: Prebuilt shared library loaded by sharp, distributed unmodified.\n",
    )
    exceptions = cl.load_exceptions(path)
    report = cl.evaluate(
        [
            cl.Package("python", "psycopg", "3", "LGPL-3.0-only"),
            cl.Package("npm", "@img/sharp-libvips-linux-x64", "1", "LGPL-3.0-or-later"),
            cl.Package("npm", "@img/sharp-libvips-darwin-arm64", "1", "LGPL-3.0-or-later"),
            cl.Package("npm", "other-lgpl", "1", "LGPL-3.0-or-later"),
        ],
        exceptions,
    )
    assert report.ok
    assert [p.name for p, _ in report.excepted] == [
        "psycopg",
        "@img/sharp-libvips-linux-x64",
        "@img/sharp-libvips-darwin-arm64",
    ]
    assert [p.name for p in report.review] == ["other-lgpl"]
    assert report.unused_exceptions == []
    # A license change upstream needs a fresh review: back to a warning.
    changed = cl.evaluate([cl.Package("python", "psycopg", "4", "LGPL-2.1-only")], exceptions)
    assert [p.name for p in changed.review] == ["psycopg"]


@pytest.mark.parametrize(
    ("ecosystem", "name"),
    [("python", "some-lib*"), ("npm", "some*thing"), ("npm", "a**"), ("npm", "*")],
)
def test_bad_exception_globs_are_rejected(tmp_path: Path, ecosystem: str, name: str) -> None:
    body = (
        f"exceptions:\n  - ecosystem: {ecosystem}\n"
        f'    name: "{name}"\n    license: LGPL-3.0\n'
        "    reason: a long enough reason for the check\n"
    )
    with pytest.raises(SystemExit):
        cl.load_exceptions(_write_exceptions(tmp_path, body))

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Unit tests for the English and Arabic check (scripts/check_translations.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_translations.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_translations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_translations"] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def _write(directory: Path, en: dict[str, object], ar: dict[str, object]) -> Path:
    (directory / "en.json").write_text(json.dumps(en), encoding="utf-8")
    (directory / "ar.json").write_text(json.dumps(ar, ensure_ascii=False), encoding="utf-8")
    return directory


def test_icu_arguments_reads_nested_plurals_and_quotes() -> None:
    found = check.icu_arguments("{count, plural, =0 {none} one {# {what}} other {# for {who}}}")
    assert found == {"count": "plural", "what": "", "who": ""}
    assert check.icu_arguments("Use '{literal}' and {name}") == {"name": ""}
    with pytest.raises(check.IcuError):
        check.icu_arguments("{broken")


def test_messages_problems_are_reported(tmp_path: Path) -> None:
    directory = _write(
        tmp_path,
        {"a": "Hi {name}", "b": "{n, plural, one {# x} other {# xs}}", "c": "x", "e": "ok"},
        {"a": "مرحبا", "b": "{n, select, a {x} other {y}}", "d": "y", "e": "a — b"},
    )
    problems = check.check_messages(directory)
    assert "messages: c is in en.json but not in ar.json" in problems
    assert "messages: d is in ar.json but not in en.json" in problems
    assert "messages: ar.json e has an em dash" in problems
    assert any(p.startswith("messages: a placeholders differ") for p in problems)
    assert "messages: b {n} is a plural in one language only" in problems


def test_arabic_plural_forms_may_differ(tmp_path: Path) -> None:
    directory = _write(
        tmp_path,
        {"n": "{count, plural, one {# day} other {# days}}"},
        {"n": "{count, plural, one {يوم} two {يومان} few {# أيام} many {# يوما} other {# يوم}}"},
    )
    assert check.check_messages(directory) == []


def test_the_repository_reads_fully_in_both_languages() -> None:
    assert check.check_messages() == []
    assert check.check_templates(check.collect_templates()) == []
    assert check.check_offer_letter() == []

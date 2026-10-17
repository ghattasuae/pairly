# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""CI runs exactly the tests `make ci-test` runs: every test file in exactly one shard."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_shards.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_shards", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_shards"] = module
    spec.loader.exec_module(module)
    return module


shards = _load()


def test_discovery_follows_pytest_settings() -> None:
    # The walker uses pytest's default file and directory patterns; if pyproject.toml
    # ever overrides them, ci_shards.py must learn the same patterns first.
    with (ROOT / "pyproject.toml").open("rb") as fh:
        ini = tomllib.load(fh)["tool"]["pytest"]["ini_options"]
    assert "python_files" not in ini
    assert "norecursedirs" not in ini
    found = shards.discover()
    assert "scripts/tests/test_ci_shards.py" in found
    assert any(p.startswith("apps/api/tests/") for p in found)
    for top in ini["testpaths"]:
        assert any(p.startswith(f"{top}/") for p in found), f"no test files under {top}"


def test_every_test_file_is_in_exactly_one_shard() -> None:
    problems = {
        path: owners for path in shards.discover() if len(owners := shards.owners(path)) != 1
    }
    assert problems == {}, (
        "each test file needs exactly one shard in scripts/ci_shards.py "
        "(no shard: CI never runs it; several: CI runs it twice)"
    )


def test_every_shard_and_glob_selects_files() -> None:
    found = shards.discover()
    for name, shard in shards.SHARDS.items():
        assert shards.files(name), f"shard {name} selects no test files"
        for glob in shard.include:
            assert any(shard.owns(p) and shards.fnmatch.fnmatchcase(p, glob) for p in found), (
                f"glob {glob} of shard {name} selects no test files"
            )


def test_workflow_matrix_lists_every_shard() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    matrix = jobs["python-tests"]["strategy"]["matrix"]["shard"]
    assert matrix == list(shards.SHARDS), "the python-tests matrix must list every shard"
    # The gate depends on every shard (through the matrix job) and on the coverage gate.
    assert {"python", "python-tests", "coverage"} <= set(jobs["ci-ok"]["needs"])
    assert jobs["coverage"]["needs"] == "python-tests"

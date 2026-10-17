# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The pytest shards CI runs in parallel, and the test files each one owns.

CI (`.github/workflows/ci.yml`, job `python-tests`) runs one job per shard, each with its
own Postgres, and a final job combines their coverage data and enforces the coverage gate
(`fail_under` in pyproject.toml) on the whole suite. `make ci-test` and `make local-test`
still run the whole suite in one go.

Every test file pytest discovers must belong to exactly one shard, or it would run twice
or never in CI. scripts/tests/test_ci_shards.py checks that, and that the workflow
matrix lists exactly the shards below.

The split follows measured run times on a hosted runner (with coverage): the API tests
are most of the time (about 4 seconds per test, each one seeds tenants in Postgres), so
they are cut into several shards by file name. Rebalance by moving globs between shards.

Usage:
    python scripts/ci_shards.py list            # shard names, one per line
    python scripts/ci_shards.py files SHARD     # that shard's test files, one per line
"""

from __future__ import annotations

import fnmatch
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

# pytest's defaults; pyproject.toml sets neither `python_files` nor `norecursedirs`
# (the shard test fails if it starts to, so this walker stays in step with pytest).
PYTHON_FILES = ("test_*.py", "*_test.py")
NORECURSE = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")


@dataclass(frozen=True)
class Shard:
    """Test files matching any `include` glob and no `exclude` glob (repo-relative)."""

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    def owns(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, g) for g in self.include) and not any(
            fnmatch.fnmatchcase(path, g) for g in self.exclude
        )


API = "apps/api/tests"

SHARDS: dict[str, Shard] = {
    # Engine, workflows, apps other than the API, SDK, scripts and evals: fast unit tests.
    "packages": Shard(
        include=(
            "packages/*",
            "apps/mcp/*",
            "apps/worker/*",
            "sdks/python/*",
            "scripts/tests/*",
            "evals/tests/*",
        )
    ),
    "api-hire-1": Shard(include=(f"{API}/test_hire_[a-p]*",)),
    "api-hire-2": Shard(include=(f"{API}/test_hire_[q-z]*",)),
    "api-pay-1": Shard(include=(f"{API}/test_pay_[a-m]*",)),
    "api-pay-2": Shard(include=(f"{API}/test_pay_[n-z]*",)),
    "api-1": Shard(include=(f"{API}/test_[a-e]*",)),
    "api-2": Shard(include=(f"{API}/test_[f-o]*",), exclude=(f"{API}/test_hire_*",)),
    "api-3": Shard(include=(f"{API}/test_[p-z]*",), exclude=(f"{API}/test_pay_*",)),
}


def configured_testpaths(root: Path = ROOT) -> list[str]:
    with (root / "pyproject.toml").open("rb") as fh:
        ini = tomllib.load(fh)["tool"]["pytest"]["ini_options"]
    return list(ini["testpaths"])


def discover(root: Path = ROOT) -> list[str]:
    """Every test file pytest collects from `testpaths`, as sorted repo-relative paths."""
    found: set[str] = set()
    for top in configured_testpaths(root):
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = [
                d for d in dirnames if not any(fnmatch.fnmatchcase(d, p) for p in NORECURSE)
            ]
            for name in filenames:
                if any(fnmatch.fnmatchcase(name, p) for p in PYTHON_FILES):
                    found.add(PurePosixPath(Path(dirpath, name).relative_to(root)).as_posix())
    return sorted(found)


def owners(path: str) -> list[str]:
    return [name for name, shard in SHARDS.items() if shard.owns(path)]


def files(shard: str, root: Path = ROOT) -> list[str]:
    return [p for p in discover(root) if SHARDS[shard].owns(p)]


def main(argv: list[str]) -> int:
    if argv == ["list"]:
        print("\n".join(SHARDS))
        return 0
    if len(argv) == 2 and argv[0] == "files" and argv[1] in SHARDS:
        selected = files(argv[1])
        if not selected:
            print(f"shard {argv[1]} has no test files", file=sys.stderr)
            return 1
        print("\n".join(selected))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

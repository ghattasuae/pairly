#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""License gate: every dependency must be compatible with AGPL-3.0 and with relicensing.

Pairly is dual licensed (AGPL-3.0 plus a commercial license from Proofly Technology,
see LICENSING.md). A dependency under a strong copyleft license would block the
commercial license, and an unknown license cannot be checked at all. This script
classifies every third-party dependency and fails the build on either.

Sources:
    Python  installed distributions in the current environment (run it with `uv run`
            after `uv sync --all-packages`), read with importlib.metadata.
    npm     `pnpm licenses list --json` for every workspace project (dev included).

Classes:
    allow   permissive or weak file-level copyleft (see ALLOW).
    review  weak copyleft that is fine when used as a separate library (see REVIEW).
            Printed as a warning, does not fail.
    deny    strong or network copyleft and source-available licenses (see DENY).
    unknown nothing we can recognise. Treated like deny.

A deny or unknown package passes only when scripts/license-exceptions.yaml lists it
with a written reason. A review package listed there is a recorded decision: it is
reported as excepted instead of warned about. An npm exception name may end in `*` to
cover platform builds of one package (`@img/sharp-libvips-*`). Our own packages
(pairly*, @pairly/*) are skipped.

SPDX expressions are understood: `A OR B` passes when either side passes, `A AND B`
only when both pass, `A WITH exception` is judged on A.

Usage:
    uv run python scripts/check_licenses.py                 # Python and npm
    uv run python scripts/check_licenses.py --only python
    uv run python scripts/check_licenses.py --pnpm-json f   # read pnpm output from a file
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCEPTIONS = ROOT / "scripts" / "license-exceptions.yaml"

OWN_PYTHON_PREFIX = "pairly"
OWN_NPM_PREFIX = "@pairly/"


class Verdict(IntEnum):
    """Ordered from best to worst, so OR takes the min and AND takes the max."""

    ALLOW = 0
    REVIEW = 1
    UNKNOWN = 2
    DENY = 3


# ---------------------------------------------------------------- policy
# Canonical SPDX ids. Keys are compared case-insensitively.
ALLOW = frozenset(
    {
        "MIT",
        "MIT-0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
        "0BSD",
        "Unlicense",
        "CC0-1.0",
        "BlueOak-1.0.0",
        "Zlib",
    }
)

# Matched by prefix (case-insensitive) after normalisation.
REVIEW_PREFIXES = ("LGPL-", "EPL-")
DENY_PREFIXES = ("GPL-", "AGPL-", "SSPL", "BUSL", "Commons-Clause", "Elastic-")

# Free-form names found in Python metadata (License field and trove classifiers) and
# in old package.json files, mapped to SPDX ids. Keys are lower case.
ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit-0": "MIT-0",
    "mit no attribution": "MIT-0",
    "expat": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "psfl": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "the unlicense": "Unlicense",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "zlib/libpng license": "Zlib",
    "blue oak model license 1.0.0": "BlueOak-1.0.0",
    "gnu lesser general public license v2 (lgplv2)": "LGPL-2.0",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.0-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.0-or-later",
    "lgpl": "LGPL-2.0-or-later",
    "eclipse public license 2.0 (epl-2.0)": "EPL-2.0",
    "gnu general public license (gpl)": "GPL-2.0-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gpl": "GPL-2.0-or-later",
    "gplv2": "GPL-2.0",
    "gplv3": "GPL-3.0",
    "gnu affero general public license v3": "AGPL-3.0",
    "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "agpl": "AGPL-3.0",
    "server side public license": "SSPL-1.0",
    "business source license": "BUSL-1.1",
}

# Trove classifiers that carry no license name.
GENERIC_CLASSIFIERS = frozenset({"osi approved", "other/proprietary license", "dfsg approved"})

# A License field longer than this is usually the full license text, not a name.
MAX_LICENSE_FIELD = 100


# ---------------------------------------------------------------- classification
def normalise(name: str) -> str:
    """Map one license name to an SPDX id when we know it, else return it trimmed."""
    cleaned = name.strip().strip("\"'").strip()
    alias = ALIASES.get(cleaned.lower())
    if alias:
        return alias
    for spdx in ALLOW:
        if spdx.lower() == cleaned.lower():
            return spdx
    return cleaned


def classify_id(license_id: str) -> Verdict:
    """Classify a single license id (no operators)."""
    spdx = normalise(license_id)
    lowered = spdx.lower()
    if "commons clause" in lowered or "commons-clause" in lowered:
        return Verdict.DENY
    if spdx in ALLOW:
        return Verdict.ALLOW
    # "Apache-2.0+" style suffixes keep the base verdict.
    base = spdx.removesuffix("+")
    if base in ALLOW:
        return Verdict.ALLOW
    if lowered.startswith(tuple(p.lower() for p in DENY_PREFIXES)):
        return Verdict.DENY
    if lowered.startswith(tuple(p.lower() for p in REVIEW_PREFIXES)):
        return Verdict.REVIEW
    return Verdict.UNKNOWN


_TOKEN = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bWITH\b|[^\s()]+", re.IGNORECASE)


class _Parser:
    """Tiny recursive descent parser for SPDX license expressions.

    expr := term (OR term)* ; term := atom (AND atom)* ; atom := '(' expr ')' | id [WITH id]
    """

    def __init__(self, text: str) -> None:
        self.tokens = _TOKEN.findall(text)
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of license expression")
        self.pos += 1
        return token

    def parse(self) -> Verdict:
        verdict = self._expr()
        if self._peek() is not None:
            raise ValueError(f"unexpected token {self._peek()!r}")
        return verdict

    def _expr(self) -> Verdict:
        verdict = self._term()
        while (token := self._peek()) is not None and token.upper() == "OR":
            self._next()
            verdict = min(verdict, self._term())
        return verdict

    def _term(self) -> Verdict:
        verdict = self._atom()
        while (token := self._peek()) is not None and token.upper() == "AND":
            self._next()
            verdict = max(verdict, self._atom())
        return verdict

    def _atom(self) -> Verdict:
        word = self._next()
        if word == "(":
            verdict = self._expr()
            if self._next() != ")":
                raise ValueError("missing closing parenthesis")
            return verdict
        if word.upper() in {"AND", "OR", "WITH", ")"}:
            raise ValueError(f"unexpected operator {word!r}")
        if (nxt := self._peek()) is not None and nxt.upper() == "WITH":
            self._next()
            self._next()  # the exception id; judged on the base license
        return classify_id(word)


def classify_expression(text: str) -> Verdict:
    """Classify a license string: an SPDX expression or one free-form license name."""
    stripped = text.strip()
    if not stripped:
        return Verdict.UNKNOWN
    lowered = stripped.lower()
    if "commons clause" in lowered or "commons-clause" in lowered:
        return Verdict.DENY
    # A whole free-form name ("BSD 3-Clause License", "Apache Software License").
    whole = normalise(stripped)
    if whole != stripped or whole in ALLOW:
        return classify_id(whole)
    # Old npm style "(MIT/Apache-2.0)" or "MIT, Apache-2.0" lists mean a choice.
    if "/" in stripped and " " not in stripped.strip("()"):
        return min(classify_expression(p) for p in stripped.strip("()").split("/"))
    try:
        return _Parser(stripped).parse()
    except ValueError:
        return Verdict.UNKNOWN


# ---------------------------------------------------------------- packages
@dataclass(frozen=True)
class Package:
    ecosystem: str  # "python" or "npm"
    name: str
    version: str
    license: str


@dataclass(frozen=True)
class LicenseException:
    ecosystem: str
    name: str
    license: str
    reason: str


@dataclass
class Report:
    allowed: list[Package] = field(default_factory=list)
    review: list[Package] = field(default_factory=list)
    excepted: list[tuple[Package, LicenseException]] = field(default_factory=list)
    failures: list[tuple[Package, Verdict]] = field(default_factory=list)
    unused_exceptions: list[LicenseException] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    return "" if value.upper() in {"UNKNOWN", "NONE"} else value


def _as_token(name: str) -> str:
    """Make an unrecognised free-form name safe to join into an SPDX expression."""
    return re.sub(r"[\s()]+", "-", name).strip("-")


def python_license(meta: Any) -> str:
    """Best license string from core metadata.

    Order: License-Expression (PEP 639), then a short License field we can recognise,
    then the trove classifiers (several classifiers are joined with OR, the usual
    meaning of a dual-licensed package), then whatever the License field holds.
    """
    expression = _clean(meta.get("License-Expression"))
    if expression:
        return expression
    raw_field = _clean(meta.get("License"))
    short_field = (
        raw_field
        if raw_field and len(raw_field) <= MAX_LICENSE_FIELD and "\n" not in raw_field
        else ""
    )
    if short_field and classify_expression(short_field) is not Verdict.UNKNOWN:
        return short_field
    classifiers = sorted(
        {
            c.split("::")[-1].strip()
            for c in (meta.get_all("Classifier") or [])
            if c.startswith("License ::")
        }
        - {"OSI Approved"}
    )
    ids = [_as_token(normalise(c)) for c in classifiers if c.lower() not in GENERIC_CLASSIFIERS]
    if ids:
        return " OR ".join(ids)
    if short_field:
        return short_field
    if raw_field:
        return raw_field.splitlines()[0][:MAX_LICENSE_FIELD]
    return ""


def python_packages(distributions: Iterable[Any] | None = None) -> list[Package]:
    seen: dict[str, Package] = {}
    for dist in distributions if distributions is not None else metadata.distributions():
        meta = dist.metadata
        name = _clean(meta.get("Name"))
        if not name:
            continue
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key.startswith(OWN_PYTHON_PREFIX):
            continue
        seen[key] = Package("python", key, str(meta.get("Version") or ""), python_license(meta))
    return sorted(seen.values(), key=lambda p: p.name)


def _license_text_for_pnpm(group_key: str, entry: dict[str, Any]) -> str:
    value = entry.get("license") or group_key
    if isinstance(value, dict):  # very old package.json: {"type": "MIT"}
        value = value.get("type", "")
    if isinstance(value, list):  # old package.json: [{"type": "MIT"}, {"type": "Apache-2.0"}]
        value = " OR ".join(str(v.get("type", v)) if isinstance(v, dict) else str(v) for v in value)
    return "" if str(value).strip().upper() == "UNKNOWN" else str(value)


def npm_packages(pnpm_json: dict[str, Any]) -> list[Package]:
    """Flatten `pnpm licenses list --json`, which groups packages by license."""
    seen: dict[tuple[str, str], Package] = {}
    for group_key, entries in pnpm_json.items():
        for entry in entries:
            name = str(entry.get("name", ""))
            if not name or name.startswith(OWN_NPM_PREFIX) or name == "pairly":
                continue
            license_text = _license_text_for_pnpm(group_key, entry)
            for version in entry.get("versions") or [""]:
                seen[(name, version)] = Package("npm", name, version, license_text)
    return sorted(seen.values(), key=lambda p: (p.name, p.version))


def run_pnpm_licenses(cwd: Path) -> dict[str, Any]:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("pnpm not found on PATH (install it, or pass --only python)")
    # -r covers every workspace project, not only the root package.json.
    out = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        [pnpm, "licenses", "list", "--json", "-r"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not out.strip():
        return {}
    data = json.loads(out)
    if not isinstance(data, dict):
        raise SystemExit("unexpected `pnpm licenses list --json` output")
    return data


# ---------------------------------------------------------------- exceptions
def load_exceptions(path: Path) -> list[LicenseException]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping with an `exceptions` list")
    items = data.get("exceptions")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise SystemExit(f"{path}: `exceptions` must be a list")
    result: list[LicenseException] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise SystemExit(f"{path}: exception #{i + 1} must be a mapping")
        missing = [k for k in ("ecosystem", "name", "license", "reason") if not item.get(k)]
        if missing:
            raise SystemExit(f"{path}: exception #{i + 1} is missing {', '.join(missing)}")
        if item["ecosystem"] not in {"python", "npm"}:
            raise SystemExit(f"{path}: exception #{i + 1}: ecosystem must be python or npm")
        if len(str(item["reason"]).strip()) < 20:
            raise SystemExit(f"{path}: exception #{i + 1}: write a real reason (20+ chars)")
        name = str(item["name"])
        if "*" in name and (
            item["ecosystem"] != "npm"
            or name.count("*") > 1
            or not name.endswith("*")
            or len(name) < 4
        ):
            raise SystemExit(f"{path}: exception #{i + 1}: only npm names may end in a single `*`")
        if item["ecosystem"] == "python":
            name = re.sub(r"[-_.]+", "-", name).lower()
        result.append(
            LicenseException(
                item["ecosystem"], name, str(item["license"]), str(item["reason"]).strip()
            )
        )
    return result


def _find_exception(
    pkg: Package, exceptions: Sequence[LicenseException]
) -> LicenseException | None:
    """Exact name first; npm names ending in `*` match by prefix."""
    for exc in exceptions:
        if exc.ecosystem == pkg.ecosystem and exc.name == pkg.name:
            return exc
    for exc in exceptions:
        if (
            exc.ecosystem == pkg.ecosystem == "npm"
            and exc.name.endswith("*")
            and pkg.name.startswith(exc.name[:-1])
        ):
            return exc
    return None


def evaluate(packages: Sequence[Package], exceptions: Sequence[LicenseException]) -> Report:
    """Classify packages. An exception matches on ecosystem, name and exact license
    string, so a license change upstream needs a fresh review."""
    report = Report()
    used: set[tuple[str, str]] = set()
    for pkg in packages:
        verdict = classify_expression(pkg.license)
        if verdict is Verdict.ALLOW:
            report.allowed.append(pkg)
            continue
        exc = _find_exception(pkg, exceptions)
        if exc is not None and exc.license == pkg.license:
            report.excepted.append((pkg, exc))
            used.add((exc.ecosystem, exc.name))
        elif verdict is Verdict.REVIEW:
            report.review.append(pkg)
        else:
            report.failures.append((pkg, verdict))
    report.unused_exceptions = [e for e in exceptions if (e.ecosystem, e.name) not in used]
    return report


# ---------------------------------------------------------------- output
def _gha() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def print_report(report: Report) -> None:
    total = len(report.allowed) + len(report.review) + len(report.excepted) + len(report.failures)
    print(f"license check: {total} third-party packages")
    print(f"  allowed:  {len(report.allowed)}")
    print(f"  review:   {len(report.review)}")
    print(f"  excepted: {len(report.excepted)}")
    print(f"  failing:  {len(report.failures)}")
    for pkg in report.review:
        line = f"review: {pkg.ecosystem} {pkg.name} {pkg.version}: {pkg.license}"
        print(f"::warning::{line}" if _gha() else f"WARNING {line}")
    for pkg, exc in report.excepted:
        print(f"excepted: {pkg.ecosystem} {pkg.name} {pkg.version}: {pkg.license} ({exc.reason})")
    for exc in report.unused_exceptions:
        line = f"unused exception: {exc.ecosystem} {exc.name} ({exc.license}); remove it"
        print(f"::warning::{line}" if _gha() else f"WARNING {line}")
    for pkg, verdict in report.failures:
        label = "denied" if verdict is Verdict.DENY else "unknown license"
        line = f"{label}: {pkg.ecosystem} {pkg.name} {pkg.version}: {pkg.license or '(none)'}"
        print(f"::error::{line}" if _gha() else f"ERROR {line}")
    if report.failures:
        print(
            "\nFix: replace the dependency, or add it to scripts/license-exceptions.yaml "
            "with a written reason after a license review."
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", choices=["python", "npm"], help="check one ecosystem")
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument(
        "--pnpm-json", type=Path, help="read pnpm licenses JSON from this file instead"
    )
    parser.add_argument("--list", action="store_true", help="print every package and license")
    args = parser.parse_args(argv)

    packages: list[Package] = []
    if args.only in (None, "python"):
        packages += python_packages()
    if args.only in (None, "npm"):
        if args.pnpm_json:
            pnpm_data = json.loads(args.pnpm_json.read_text(encoding="utf-8"))
        else:
            pnpm_data = run_pnpm_licenses(ROOT)
        packages += npm_packages(pnpm_data)

    if args.list:
        for pkg in packages:
            verdict = classify_expression(pkg.license).name.lower()
            print(f"{verdict:8} {pkg.ecosystem:6} {pkg.name} {pkg.version}: {pkg.license}")

    report = evaluate(packages, load_exceptions(args.exceptions))
    print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

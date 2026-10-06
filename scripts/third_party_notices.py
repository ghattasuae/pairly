#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Generate NOTICE.md: every third-party component and its license.

Usage:
    uv run python scripts/third_party_notices.py                 # print to stdout
    uv run python scripts/third_party_notices.py -o NOTICE.md    # write the file

Python packages come from the current environment (importlib.metadata, the same
data `uv pip list --format json` shows). JavaScript packages come from
pnpm-lock.yaml when it exists, with license data read from the installed
package.json files under node_modules. Missing data is shown as UNKNOWN; the
script never fails because one package lacks metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

UNKNOWN = "UNKNOWN"

# Our own workspace packages are AGPL-3.0 and are not third-party components.
OWN_PREFIXES = ("pairly", "@pairly/")

# A License field longer than this is usually the full license text, not a name.
MAX_LICENSE_FIELD = 80


@dataclass(frozen=True, order=True)
class Component:
    name: str
    version: str
    license: str


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.upper() in {"UNKNOWN", "NONE", ""}:
        return ""
    return value


def python_license(meta: metadata.PackageMetadata) -> str:
    """Best license string from package metadata, in order of reliability."""
    expression = _clean(meta.get("License-Expression"))
    if expression:
        return expression
    field = _clean(meta.get("License"))
    if field and len(field) <= MAX_LICENSE_FIELD and "\n" not in field:
        return field
    classifiers = [
        c.split("::")[-1].strip()
        for c in (meta.get_all("Classifier") or [])
        if c.startswith("License ::") and c.strip() != "License :: OSI Approved"
    ]
    if classifiers:
        return ", ".join(sorted(set(classifiers)))
    if field:
        # Full license text: keep only the first line as a hint.
        return field.splitlines()[0][:MAX_LICENSE_FIELD]
    return UNKNOWN


def python_components() -> list[Component]:
    seen: dict[str, Component] = {}
    for dist in metadata.distributions():
        try:
            meta = dist.metadata
            name = _clean(meta.get("Name"))
        except Exception:  # noqa: S112 - broken metadata must not stop the report
            continue
        if not name or name.lower().startswith(OWN_PREFIXES):
            continue
        try:
            license_name = python_license(meta)
        except Exception:
            license_name = UNKNOWN
        key = name.lower()
        if key not in seen:
            seen[key] = Component(name, dist.version or UNKNOWN, license_name)
    return sorted(seen.values(), key=lambda c: c.name.lower())


# pnpm lockfile keys under `packages:`. v9: `name@1.0.0:` or `'@scope/name@1.0.0':`.
# v6: `/name@1.0.0:` or `/@scope/name@1.0.0(peer@2.0.0):`.
_LOCK_KEY_RE = re.compile(r"^  (?P<key>['\"]?/?[^\s:'\"][^:]*?['\"]?):\s*$")


def parse_pnpm_key(raw: str) -> tuple[str, str] | None:
    key = raw.strip().strip("'\"").lstrip("/")
    key = key.split("(", 1)[0]  # drop peer dependency suffixes
    at = key.rfind("@")
    if at <= 0:
        return None
    name, version = key[:at], key[at + 1 :]
    if not name or not version:
        return None
    return name, version


def pnpm_packages(lockfile: Path) -> list[tuple[str, str]]:
    """(name, version) pairs from the `packages:` section of pnpm-lock.yaml."""
    found: set[tuple[str, str]] = set()
    in_packages = False
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(" "):
            in_packages = line.rstrip() == "packages:"
            continue
        if not in_packages:
            continue
        match = _LOCK_KEY_RE.match(line)
        if match:
            parsed = parse_pnpm_key(match.group("key"))
            if parsed:
                found.add(parsed)
    return sorted(found, key=lambda p: (p[0].lower(), p[1]))


def _license_from_package_json(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    if isinstance(value, str):
        return _clean(value)
    licenses = data.get("licenses")
    if isinstance(licenses, list):
        names = [item.get("type") for item in licenses if isinstance(item, dict)]
        return " OR ".join(n for n in names if isinstance(n, str))
    return ""


def js_license(root: Path, name: str, version: str) -> str:
    store_name = name.replace("/", "+")
    candidates: tuple[Path, ...] = (
        root / "node_modules" / ".pnpm" / f"{store_name}@{version}" / "node_modules" / name,
        root / "node_modules" / name,
    )
    for directory in candidates:
        found = _license_from_package_json(directory / "package.json")
        if found:
            return found
    # pnpm store folders may carry a peer suffix, for example name@1.0.0_react@19.0.0.
    store = root / "node_modules" / ".pnpm"
    if store.is_dir():
        for directory in sorted(store.glob(f"{store_name}@{version}_*")):
            found = _license_from_package_json(directory / "node_modules" / name / "package.json")
            if found:
                return found
    installed = any((d / "package.json").is_file() for d in candidates) or (
        store.is_dir() and any(store.glob(f"{store_name}@{version}*"))
    )
    # Optional platform binaries for other systems are listed in the lockfile but not installed.
    return UNKNOWN if installed else f"{UNKNOWN} (not installed here)"


def js_components(root: Path) -> list[Component] | None:
    lockfile = root / "pnpm-lock.yaml"
    if not lockfile.is_file():
        return None
    components = []
    for name, version in pnpm_packages(lockfile):
        if name.lower().startswith(OWN_PREFIXES):
            continue
        components.append(Component(name, version, js_license(root, name, version)))
    return components


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def table(components: list[Component]) -> str:
    rows = ["| Name | Version | License |", "| --- | --- | --- |"]
    rows += [f"| {_cell(c.name)} | {_cell(c.version)} | {_cell(c.license)} |" for c in components]
    return "\n".join(rows)


def render(python: list[Component], js: list[Component] | None) -> str:
    parts = [
        "# Third-party notices",
        "",
        "Pairly AI is open source (AGPL-3.0) with a commercial license option. "
        "It uses the third-party components listed below. Each one stays under its own license.",
        "",
        "This file is generated by `scripts/third_party_notices.py`. Do not edit it by hand. "
        "To refresh it, install all dependencies and run:",
        "",
        "```sh",
        "uv run python scripts/third_party_notices.py -o NOTICE.md",
        "```",
        "",
        "A license shown as UNKNOWN means the package did not publish license metadata, "
        "or it is not installed on this machine. Check it by hand before release.",
        "",
        f"## Python packages ({len(python)})",
        "",
        table(python) if python else "No Python packages found in the current environment.",
        "",
    ]
    if js is None:
        parts += ["## JavaScript packages", "", "No pnpm-lock.yaml found.", ""]
    else:
        parts += [
            f"## JavaScript packages ({len(js)})",
            "",
            table(js) if js else "No packages listed in pnpm-lock.yaml.",
            "",
        ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate NOTICE.md")
    parser.add_argument("-o", "--output", type=Path, help="file to write (default: stdout)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)

    text = render(python_components(), js_components(args.root))
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

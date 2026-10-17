#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Generate NOTICE.md: every third-party component and its license.

Usage:
    uv run python scripts/third_party_notices.py                 # print to stdout
    uv run python scripts/third_party_notices.py -o NOTICE.md    # write the file
    uv run python scripts/third_party_notices.py --fetch-missing -o NOTICE.md
        # also ask PyPI and the npm registry for packages not installed here (optional
        # platform binaries for other systems)

Python packages come from the current environment (importlib.metadata, the same
data `uv pip list --format json` shows). JavaScript packages come from
pnpm-lock.yaml when it exists, with license data read from the installed
package.json files under node_modules. Missing data is shown as UNKNOWN; the
script never fails because one package lacks metadata.

Bundled components that are not packages of their own (PDFium inside the pypdfium2
wheels, the pdf.js worker and fonts the web app serves, the DejaVu Sans subset embedded
in decision receipts, the disposable email domain list of Pairly Hire, the vendored
Redoc bundle of ``make api-docs`` with React and DOMPurify inside it, the caniuse-lite
data) are listed in their own section with links to their license texts.

The script exits 1 and writes nothing when a known bundled asset (KNOWN_ASSETS) is
shipped but missing from the output, when a linked license text does not exist, or
when a committed asset that looks third party (a font, a vendored or minified bundle,
OCR language data) is not covered by any bundled entry.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

UNKNOWN = "UNKNOWN"

_NOT_INSTALLED = f"{UNKNOWN} (not installed here)"

# Our own workspace packages (dual licensed or Apache-2.0) are not third-party components.
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


def uv_lock_packages(lockfile: Path) -> list[tuple[str, str]]:
    """(name, version) of every package in uv.lock (all platforms)."""
    import tomllib

    try:
        data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = []
    for package in data.get("package", []):
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            found.append((name, version))
    return found


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def with_locked_python(installed: list[Component], root: Path) -> list[Component]:
    """Add uv.lock packages that are not installed here (other platforms only)."""
    have = {_norm(c.name) for c in installed}
    extra = [
        Component(name, version, _NOT_INSTALLED)
        for name, version in uv_lock_packages(root / "uv.lock")
        if _norm(name) not in have and not name.lower().startswith(OWN_PREFIXES)
    ]
    return sorted([*installed, *extra], key=lambda c: c.name.lower())


def pypi_license(name: str, version: str, *, timeout: float = 15.0) -> str:
    """The license a PyPI release declares (expression, field or classifiers), or ""."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            info = json.loads(response.read().decode("utf-8")).get("info") or {}
    except (OSError, ValueError):
        return ""
    for key in ("license_expression", "license"):
        value = _clean(info.get(key))
        if value and len(value) <= MAX_LICENSE_FIELD and "\n" not in value:
            return value
    classifiers = [
        c.split("::")[-1].strip()
        for c in info.get("classifiers") or []
        if c.startswith("License ::") and c.strip() != "License :: OSI Approved"
    ]
    return ", ".join(sorted(set(classifiers)))


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
    return UNKNOWN if installed else _NOT_INSTALLED


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


NPM_REGISTRY = "https://registry.npmjs.org"


def registry_license(name: str, version: str, *, timeout: float = 15.0) -> str:
    """The license an npm package version declares in the public registry, or ""."""
    url = f"{NPM_REGISTRY}/{urllib.parse.quote(name, safe='@')}/{urllib.parse.quote(version)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https host
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    return _clean(value) if isinstance(value, str) else ""


def fill_from_registry(
    components: list[Component], lookup: Callable[[str, str], str] = registry_license
) -> list[Component]:
    """Ask a registry for every package that is not installed on this machine."""
    missing = [c for c in components if c.license == _NOT_INSTALLED]
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(
            zip(missing, pool.map(lambda c: lookup(c.name, c.version), missing), strict=True)
        )
    return [
        Component(c.name, c.version, f"{found[c]} (from the registry)") if found.get(c) else c
        for c in components
    ]


# ---------------------------------------------------------------- bundled components
# Third-party code, fonts and data that ship inside Pairly or inside one of its
# packages, rather than as a package of their own. Each entry links the license texts
# a recipient must receive; texts that do not already sit next to the asset are kept
# under LICENSES/third-party/.

THIRD_PARTY = "LICENSES/third-party"
REDOC_VERSION = "2.5.4"
REDOC_DIR = "infra/api-docs/vendor/redoc"
FONT_DIR = "packages/core/pairly_core/decisions/fonts"
DISPOSABLE_DOMAINS = "packages/hire/pairly_hire/data/disposable_domains.txt"
PDFJS_COPY_SCRIPT = "apps/web/scripts/copy-pdfjs.mjs"

# Committed files that look like third-party assets. Every match must be covered by a
# bundled entry (Bundled.covers), so a new font, vendored bundle or OCR language file
# cannot slip in without a notice.
ASSET_PATTERNS: tuple[str, ...] = (
    "*.ttf",
    "*.otf",
    "*.woff",
    "*.woff2",
    "*.pfb",
    "*.traineddata",
    "*.min.js",
    "*.wasm",
    "*/vendor/*",
)


@dataclass(frozen=True)
class Bundled:
    key: str
    name: str
    version: str
    license: str
    where: str
    # (label, target): a repository path relative to the root, or an https URL.
    texts: tuple[tuple[str, str], ...] = ()
    # Repository path patterns (fnmatch) of committed files this entry accounts for.
    covers: tuple[str, ...] = ()


def _pdfium_version() -> str:
    try:
        from pypdfium2.version import PDFIUM_INFO  # type: ignore[import-untyped]
    except Exception:
        return UNKNOWN
    return str(PDFIUM_INFO)


def _locked_js_version(root: Path, name: str) -> str:
    lockfile = root / "pnpm-lock.yaml"
    if not lockfile.is_file():
        return UNKNOWN
    versions = sorted({v for n, v in pnpm_packages(lockfile) if n == name})
    return ", ".join(versions) if versions else UNKNOWN


def bundled_components(root: Path | None = None) -> list[Bundled]:
    """Third-party code and data shipped inside other packages or in this repository."""
    root = root if root is not None else Path(__file__).resolve().parents[1]
    pdfium_texts = (
        *(
            (label, f"{THIRD_PARTY}/pdfium/{file}")
            for label, file in (
                ("PDFium", "pdfium.txt"),
                ("pdfium-binaries", "pdfium-binaries.txt"),
                ("pypdfium2 (BSD-3-Clause)", "pypdfium2-BSD-3-Clause.txt"),
                ("FreeType", "freetype.txt"),
                ("ICU", "icu.txt"),
                ("libjpeg-turbo", "libjpeg_turbo.md"),
                ("libjpeg-turbo (IJG)", "libjpeg_turbo.ijg"),
                ("lcms", "lcms.txt"),
                ("OpenJPEG", "libopenjpeg.txt"),
                ("libpng", "libpng.txt"),
                ("zlib", "zlib.txt"),
                ("abseil", "abseil.txt"),
                ("Anti-Grain Geometry", "agg23.txt"),
                ("fast_float", "fast_float.txt"),
                ("simdutf", "simdutf.txt"),
                ("LLVM libc", "llvm-libc.txt"),
            )
        ),
        ("Apache-2.0", "LICENSES/Apache-2.0.txt"),
        ("CC-BY-4.0", f"{THIRD_PARTY}/CC-BY-4.0.txt"),
    )
    return [
        Bundled(
            "pdfium",
            "PDFium",
            _pdfium_version(),
            "BSD-3-Clause; its bundled build also carries FreeType (FTL), ICU (Unicode "
            "license), libjpeg-turbo (IJG and BSD-3-Clause), lcms (MIT), OpenJPEG "
            "(BSD-2-Clause), libpng, zlib, Anti-Grain Geometry, abseil (Apache-2.0), "
            "fast_float and simdutf (MIT or Apache-2.0), LLVM libc (Apache-2.0 WITH "
            "LLVM-exception)",
            "compiled into the pypdfium2 wheels (pypdfium2 is BSD-3-Clause OR Apache-2.0, "
            "its docs CC-BY-4.0); none of the bundled projects ships an Apache NOTICE file",
            pdfium_texts,
        ),
        Bundled(
            "pdfjs",
            "pdf.js (pdfjs-dist worker and standard fonts)",
            _locked_js_version(root, "pdfjs-dist"),
            "Apache-2.0 (pdf.js ships no NOTICE file); standard fonts: Foxit fonts "
            "(BSD-3-Clause, PDFium Authors), Liberation Sans (GPL-2.0 with a font "
            "embedding exception)",
            f"{PDFJS_COPY_SCRIPT} copies the worker, the fonts and their license files into "
            "apps/web/public/pdfjs/ at build time; the web app serves them",
            (
                ("Apache-2.0", "LICENSES/Apache-2.0.txt"),
                ("Foxit fonts", f"{THIRD_PARTY}/pdfjs/LICENSE_FOXIT.txt"),
                ("Liberation fonts", f"{THIRD_PARTY}/pdfjs/LICENSE_LIBERATION.txt"),
            ),
        ),
        Bundled(
            "dejavu",
            "DejaVu Sans (subset)",
            "2.37",
            "Bitstream Vera Fonts License and Arev Fonts License; DejaVu changes public domain",
            f"{FONT_DIR}/, embedded in decision receipt PDFs",
            (("Bitstream Vera and Arev licenses", f"{FONT_DIR}/LICENSE_DEJAVU"),),
            (f"{FONT_DIR}/*",),
        ),
        Bundled(
            "disposable-domains",
            "disposable-email-domains (subset)",
            "curated 2026-10",
            "CC0-1.0",
            f"{DISPOSABLE_DOMAINS}, the domains the Hire apply form refuses",
            (
                ("CC0-1.0", "https://creativecommons.org/publicdomain/zero/1.0/legalcode"),
                (
                    "upstream",
                    "https://github.com/disposable-email-domains/disposable-email-domains",
                ),
            ),
            (DISPOSABLE_DOMAINS,),
        ),
        Bundled(
            "redoc",
            "Redoc (standalone bundle)",
            REDOC_VERSION,
            "MIT; the bundle also carries React (MIT), DOMPurify (Apache-2.0 OR MPL-2.0) and "
            "other MIT and ISC code, see redoc.standalone.js.LICENSE.txt",
            f"{REDOC_DIR}/, the API reference page make api-docs writes",
            (
                ("MIT", f"{REDOC_DIR}/LICENSE"),
                ("bundled notices", f"{REDOC_DIR}/redoc.standalone.js.LICENSE.txt"),
            ),
            (f"{REDOC_DIR}/*",),
        ),
        Bundled(
            "react",
            "React (inside the Redoc bundle)",
            "as bundled by Redoc " + REDOC_VERSION,
            "MIT",
            f"{REDOC_DIR}/redoc.standalone.js",
            (("MIT", f"{THIRD_PARTY}/react-MIT.txt"),),
        ),
        Bundled(
            "dompurify",
            "DOMPurify (inside the Redoc bundle)",
            "3.4.13",
            "Apache-2.0 OR MPL-2.0 (Pairly uses it under Apache-2.0; DOMPurify ships no "
            "NOTICE file)",
            f"{REDOC_DIR}/redoc.standalone.js",
            (("Apache-2.0", "LICENSES/Apache-2.0.txt"),),
        ),
        Bundled(
            "caniuse-lite",
            "caniuse-lite (browser support data from caniuse.com)",
            _locked_js_version(root, "caniuse-lite"),
            "CC-BY-4.0, data by Alexis Deveria and the caniuse.com contributors",
            "read at build time by browserslist (Next.js, PostCSS, Babel); attribution only",
            (("CC-BY-4.0", f"{THIRD_PARTY}/CC-BY-4.0.txt"),),
        ),
    ]


def _repo_files(root: Path) -> list[str]:
    """Committed (and not ignored) files, relative to root; a plain walk outside git."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        names = [name for name in out.decode("utf-8").split("\0") if name]
        if names:
            return names
    except (OSError, subprocess.CalledProcessError):
        pass
    skip = {".git", "node_modules", ".venv", "__pycache__"}
    return [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not skip.intersection(p.relative_to(root).parts)
    ]


def _bundle_mentions(root: Path, needle: str) -> bool:
    notices_file = root / REDOC_DIR / "redoc.standalone.js.LICENSE.txt"
    try:
        return needle in notices_file.read_text(encoding="utf-8")
    except OSError:
        return False


def _uv_locked(root: Path, name: str) -> bool:
    return any(_norm(n) == name for n, _ in uv_lock_packages(root / "uv.lock"))


def _js_locked(root: Path, name: str) -> bool:
    lockfile = root / "pnpm-lock.yaml"
    return lockfile.is_file() and any(n == name for n, _ in pnpm_packages(lockfile))


# Known bundled assets: key -> how to tell that this repository ships it. When the
# detector finds the asset, the generated NOTICE.md must list it.
KNOWN_ASSETS: dict[str, Callable[[Path], bool]] = {
    "redoc": lambda root: (root / REDOC_DIR / "redoc.standalone.js").is_file(),
    "react": lambda root: _bundle_mentions(root, "@license React"),
    "dompurify": lambda root: _bundle_mentions(root, "DOMPurify"),
    "dejavu": lambda root: (root / FONT_DIR / "DejaVuSans.ttf").is_file(),
    "disposable-domains": lambda root: (root / DISPOSABLE_DOMAINS).is_file(),
    "pdfjs": lambda root: (root / PDFJS_COPY_SCRIPT).is_file() or _js_locked(root, "pdfjs-dist"),
    "pdfium": lambda root: _uv_locked(root, "pypdfium2"),
    "caniuse-lite": lambda root: _js_locked(root, "caniuse-lite"),
}


def validate(root: Path, bundled: list[Bundled], text: str) -> list[str]:
    """Problems that must block NOTICE.md: a known asset missing, a license text missing,
    or a committed third-party asset that no entry covers."""
    problems: list[str] = []
    by_key = {b.key: b for b in bundled}
    for key, detect in KNOWN_ASSETS.items():
        if not detect(root):
            continue
        entry = by_key.get(key)
        if entry is None or f"| {_cell(entry.name)} |" not in text:
            problems.append(f"bundled asset {key!r} is shipped but missing from the notices")
    for entry in bundled:
        if not entry.texts:
            problems.append(f"{entry.name}: no license text linked")
        for label, target in entry.texts:
            if target.startswith("https://"):
                continue
            if not (root / target).is_file():
                problems.append(f"{entry.name}: license text {label!r} not found at {target}")
    covered = [pattern for entry in bundled for pattern in entry.covers]
    for name in _repo_files(root):
        if name.startswith(("LICENSES/", "node_modules/")) or "/node_modules/" in name:
            continue
        if not any(fnmatch.fnmatch(name, p) for p in ASSET_PATTERNS):
            continue
        if not any(fnmatch.fnmatch(name, p) for p in covered):
            problems.append(f"{name}: third-party asset not listed in the bundled components")
    return problems


def _link(label: str, target: str) -> str:
    return f"[{label}]({target.replace(' ', '%20')})"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def table(components: list[Component]) -> str:
    rows = ["| Name | Version | License |", "| --- | --- | --- |"]
    rows += [f"| {_cell(c.name)} | {_cell(c.version)} | {_cell(c.license)} |" for c in components]
    return "\n".join(rows)


def bundled_table(items: list[Bundled]) -> str:
    rows = [
        "| Name | Version | License | License texts | Where |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows += [
        f"| {_cell(b.name)} | {_cell(b.version)} | {_cell(b.license)} | "
        f"{_cell(', '.join(_link(label, target) for label, target in b.texts))} | "
        f"{_cell(b.where)} |"
        for b in items
    ]
    return "\n".join(rows)


def render(
    python: list[Component], js: list[Component] | None, bundled: list[Bundled] | None = None
) -> str:
    parts = [
        "# Third-party notices",
        "",
        "Pairly AI is open source with a commercial license option: the server and the web "
        "app are `AGPL-3.0-only OR LicenseRef-Proofly-Commercial`, the SDKs and the "
        "quickstart examples are `Apache-2.0` (see LICENSING.md). "
        "It uses the third-party components listed below. Each one stays under its own license.",
        "",
        "This file is generated by `scripts/third_party_notices.py`. Do not edit it by hand. "
        "To refresh it, install all dependencies and run:",
        "",
        "```sh",
        "uv run python scripts/third_party_notices.py --fetch-missing -o NOTICE.md",
        "```",
        "",
        "The script fails when a known bundled asset is missing from this file, when a "
        "linked license text is missing, or when a committed font, vendored bundle or "
        "similar asset is not listed below.",
        "",
        "A license shown as UNKNOWN means the package did not publish license metadata, "
        "or it is not installed on this machine. Check it by hand before release.",
        "",
        f"## Bundled components ({len(bundled or [])})",
        "",
        "Code, fonts and data shipped inside Pairly or inside one of its packages. The "
        "license texts these components require to be passed on are linked in each row; "
        "texts that do not sit next to the component are in `LICENSES/third-party/`.",
        "",
        bundled_table(bundled or []),
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
    parser.add_argument(
        "--fetch-missing",
        action="store_true",
        help="look up packages that are not installed here in the npm registry",
    )
    args = parser.parse_args(argv)

    python = with_locked_python(python_components(), args.root)
    js = js_components(args.root)
    if args.fetch_missing:
        python = fill_from_registry(python, pypi_license)
        if js is not None:
            js = fill_from_registry(js)
    bundled = bundled_components(args.root)
    text = render(python, js, bundled)
    problems = validate(args.root, bundled, text)
    if problems:
        for problem in problems:
            print(f"ERROR {problem}", file=sys.stderr)
        print(
            "Fix: add the asset to bundled_components() in scripts/third_party_notices.py "
            "with its license texts (under LICENSES/third-party/ when they do not ship "
            "next to it). Nothing was written.",
            file=sys.stderr,
        )
        return 1
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

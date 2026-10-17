#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Write the API reference: the OpenAPI JSON and a static, self-contained Redoc page.

    uv run python scripts/build_api_docs.py                 # from the API code (no server)
    uv run python scripts/build_api_docs.py --spec packages/shared-ts/openapi/v1.json
    make api-docs                                           # the same, into dist/api-docs

Writes ``<out>/openapi.json`` and ``<out>/index.html``. The page embeds the vendored
Redoc bundle (``infra/api-docs/vendor/redoc``, MIT, as a ``data:`` URL) and the spec, and its
Content Security
Policy allows no network access at all (``connect-src 'none'``, images and fonts only
from ``data:``), so opening it, even from disk, fetches nothing. Redoc's own optional
logo image is blocked by that policy and hides itself.

The build refuses a bundle whose SHA-256 is not the reviewed one (``REDOC_SHA256``, also
in the vendor README): a changed or swapped file never reaches the page (security
review 2026-10, L-11).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = ROOT / "infra" / "api-docs" / "vendor" / "redoc"
DUMP: Final = ROOT / "packages" / "shared-ts" / "scripts" / "dump_openapi.py"
DEFAULT_OUT: Final = ROOT / "dist" / "api-docs"
# SHA-256 of the reviewed ``redoc.standalone.js`` (Redoc 2.5.4, see the vendor README).
REDOC_SHA256: Final = "dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b"


class BundleChecksumError(SystemExit):
    """The vendored Redoc bundle is not the reviewed one: nothing is written."""


def read_bundle(path: Path | None = None, expected: str = REDOC_SHA256) -> str:
    """The vendored bundle as text, only when its SHA-256 is the reviewed one."""
    raw = (path or VENDOR / "redoc.standalone.js").read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise BundleChecksumError(
            f"the vendored Redoc bundle's SHA-256 is {actual}, not the reviewed {expected}: "
            "review the new bundle and update REDOC_SHA256 (infra/api-docs/vendor/redoc/README.md)"
        )
    return raw.decode("utf-8")


CSP: Final = "; ".join(
    [
        "default-src 'none'",
        # Redoc is a data: URL and the start-up code inline; it compiles JSON schemas at
        # run time (eval).
        "script-src data: 'unsafe-inline' 'unsafe-eval'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "font-src data:",
        "worker-src blob:",
        "connect-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
)

OPTIONS: Final = {
    "hideHostname": False,
    "pathInMiddlePanel": True,
    "requiredPropsFirst": True,
    "sortTagsAlphabetically": True,
    "theme": {"typography": {"fontFamily": "system-ui, sans-serif"}},
}


def schema_from_code() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("dump_openapi", DUMP)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {DUMP}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema: dict[str, Any] = module.build_schema()
    return schema


def _inline_json(value: Any) -> str:
    """JSON safe inside a <script> element: every "<" is escaped (\\u003c), so nothing in
    the spec can close the element or open an HTML comment."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")


def render_page(schema: dict[str, Any], redoc_js: str) -> str:
    info = schema.get("info", {})
    title = f"{info.get('title', 'Pairly API')} {info.get('version', '')}".strip()
    # The bundle goes in as a data: URL, so no sequence inside it ("</script>", "<!--")
    # can change how the HTML around it is parsed.
    bundle = base64.b64encode(redoc_js.encode("utf-8")).decode("ascii")
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="referrer" content="no-referrer">',
            f"<title>{html.escape(title)} reference</title>",
            "<style>body { margin: 0; }</style>",
            "</head>",
            "<body>",
            '<div id="redoc"></div>',
            f'<script type="application/json" id="openapi">{_inline_json(schema)}</script>',
            f'<script src="data:text/javascript;base64,{bundle}"></script>',
            "<script>",
            "Redoc.init(",
            '  JSON.parse(document.getElementById("openapi").textContent),',
            f"  {json.dumps(OPTIONS)},",
            '  document.getElementById("redoc"),',
            ");",
            "</script>",
            "</body>",
            "</html>",
            "",
        ]
    )


def build(out: Path, spec_path: Path | None = None) -> tuple[Path, Path]:
    # First, before anything is written: a bundle that is not the reviewed one stops the
    # build.
    redoc_js = read_bundle()
    if spec_path is not None:
        schema: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    else:
        schema = schema_from_code()
    out.mkdir(parents=True, exist_ok=True)
    spec_file = out / "openapi.json"
    spec_file.write_text(
        json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    page = out / "index.html"
    page.write_text(render_page(schema, redoc_js), encoding="utf-8")
    shutil.copyfile(VENDOR / "LICENSE", out / "redoc-LICENSE.txt")
    shutil.copyfile(VENDOR / "redoc.standalone.js.LICENSE.txt", out / "redoc-THIRD-PARTY.txt")
    return spec_file, page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--spec", type=Path, help="an OpenAPI JSON file instead of the code")
    args = parser.parse_args(argv)
    spec_file, page = build(args.out, args.spec)
    size = page.stat().st_size // 1024
    print(f"wrote {spec_file} and {page} ({size} KiB, no network access at run time)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

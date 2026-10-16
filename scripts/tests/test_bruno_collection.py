# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Bruno collection (infra/bruno) follows the API: every request is a route of the
OpenAPI document with that method, "person" requests use the person token, and no
credential is committed."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRUNO = ROOT / "infra" / "bruno"
SPEC = ROOT / "packages" / "shared-ts" / "openapi" / "v1.json"
METHOD_RE = re.compile(r"^(get|post|put|patch|delete) \{\n  url: (\S+)\n", re.M)


def _requests() -> list[Path]:
    return sorted(p for p in BRUNO.rglob("*.bru") if p.parent.name != "environments")


def _routes() -> set[tuple[str, str]]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, ops in spec["paths"].items()
        for method in ops
    }


def test_every_request_is_an_api_route() -> None:
    routes = _routes()
    requests = [p for p in _requests() if p.name != "collection.bru"]
    assert len(requests) >= 25
    for path in requests:
        text = path.read_text(encoding="utf-8")
        found = METHOD_RE.search(text)
        assert found is not None, path
        method, url = found.group(1).upper(), found.group(2)
        assert url.startswith("{{baseUrl}}/"), path
        route = url.removeprefix("{{baseUrl}}").split("?", 1)[0]
        template = re.sub(r"\{\{[A-Za-z]+\}\}", "{}", route)
        assert (method, template) in routes, (path.name, method, template)


def test_person_requests_use_the_person_token() -> None:
    for path in _requests():
        text = path.read_text(encoding="utf-8")
        if "auth: bearer" in text:
            assert "token: {{token}}" in text, path
    decide = (BRUNO / "04 Approvals" / "Decide person only.bru").read_text(encoding="utf-8")
    assert "auth: bearer" in decide


def test_no_credentials_are_committed() -> None:
    secret = re.compile(r"pk_(live|test)_[a-z0-9]{8}_|whsec_[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{10,}\.")
    for path in BRUNO.rglob("*"):
        if path.is_file() and path.suffix in {".bru", ".json", ".md"}:
            assert not secret.search(path.read_text(encoding="utf-8")), path
    for env in (BRUNO / "environments").glob("*.bru"):
        text = env.read_text(encoding="utf-8")
        assert "vars:secret [\n  apiKey,\n  token\n]" in text, env
        assert re.search(r"^\s+(apiKey|token):", text, re.M) is None, env

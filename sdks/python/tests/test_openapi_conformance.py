# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Proofly Technology
"""The hand-written client matches the OpenAPI document (packages/shared-ts/openapi/v1.json).

Every route a helper calls exists with that method, and every key of the typed responses
is a property of the schema the route returns. Regenerate the snapshot with
``pnpm gen:api`` after an API change; this test then says what the SDK must follow.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from pairly import types

SPEC = Path(__file__).resolve().parents[3] / "packages" / "shared-ts" / "openapi" / "v1.json"
CLIENT = Path(__file__).resolve().parents[1] / "pairly" / "client.py"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SPEC.read_text(encoding="utf-8"))
    return loaded


def _template(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_every_route_the_client_calls_exists(spec: dict[str, Any]) -> None:
    source = CLIENT.read_text(encoding="utf-8")
    calls = set(re.findall(r'Call\(\s*"(GET|POST)",\s*f?"(/v1/[^"]+)"', source))
    calls |= {("GET", p) for p in re.findall(r'f"(/v1/[^"]+)"', source)}
    calls |= {("POST", "/v1/documents/batch"), ("GET", "/v1/approvals")}
    known = {
        (method.upper(), _template(path)) for path, ops in spec["paths"].items() for method in ops
    }
    assert calls
    for method, path in calls:
        assert (method, _template(path)) in known, (method, path)


@pytest.mark.parametrize(
    ("typed", "schema"),
    [
        (types.Batch, "BatchOut"),
        (types.BatchItem, "BatchItemOut"),
        (types.Approval, "ApprovalOut"),
        (types.ApprovalPage, "InboxOut"),
        (types.Step, "StepOut"),
        (types.Proposal, "ProposalOut"),
        (types.ProposalPage, "ProposalList"),
    ],
)
def test_typed_responses_match_the_schemas(spec: dict[str, Any], typed: type, schema: str) -> None:
    properties = set(spec["components"]["schemas"][schema]["properties"])
    assert set(get_type_hints(typed)) == properties

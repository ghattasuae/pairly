# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The structural evals (``python -m evals.structural``): every check counts what it should,
and the committed result file is what a run gives today."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from evals.structural import run as structural
from evals.structural.run import COMPONENTS, RESULTS, evaluate


def _entry(response: dict[str, Any], *, hash_: str, inputs: dict[str, Any]) -> dict[str, Any]:
    from pairly_core.llm.hashing import schema_hash
    from pairly_hire.messaging.drafting import MessageDraft

    return {
        "task": "hire_message_draft",
        "schema_name": "MessageDraft",
        "schema_hash": schema_hash(MessageDraft),
        "request_hash": hash_ * 64,
        "request": {"inputs": inputs},
        "response": response,
    }


def test_each_check_counts_what_it_should(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structural, "LIVE", ())
    inputs = {"job_title": "Support Lead", "purpose": "status update", "language": "en"}
    bundle = {
        "format": "pairly.model-recordings",
        "version": 1,
        "entries": [
            _entry({"personal_note": "Thank you for your patience."}, hash_="a", inputs=inputs),
            _entry({"personal_note": "Hi", "extra": 1}, hash_="b", inputs=inputs),
            _entry({"personal_note": "Click <script>x</script> now."}, hash_="c", inputs=inputs),
            _entry({"personal_note": "Thanks — we will write soon."}, hash_="d", inputs=inputs),
            _entry({"personal_note": "We will answer within 3 days."}, hash_="e", inputs=inputs),
        ],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    base = next(c for c in COMPONENTS if c.slug == "message-personalisation")
    got = evaluate(dataclasses.replace(base, sources=(str(path),)))
    assert got["status"] == "measured"
    assert got["recorded_outputs"] == 5
    assert (got["schema_valid"]["rate"], got["schema_valid"]["failed"]) == (0.8, 1)
    assert got["banned_content_free"]["failed"] == 1
    assert got["banned_content_free"]["failures"][0]["problems"] == ["banned:markup"]
    assert got["em_dash_free"]["failed"] == 1
    # The product's own check refuses the markup, the em dash and the invented number.
    assert got["product_check"]["name"] == "no_invented_facts"
    assert got["product_check"]["n"] == 4
    assert got["product_check"]["failed"] >= 2
    assert got["schema_current"]["rate"] == 1.0
    assert got["cost"]["status"] == "not_measured"
    assert got["provenance"] == "scripted answers"


def test_a_component_without_recordings_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structural, "LIVE", ())
    got = evaluate(next(c for c in COMPONENTS if c.slug == "policy-rag-check"))
    assert (got["status"], got["recorded_outputs"]) == ("no_recordings", 0)
    assert "schema_valid" not in got  # nothing measured, nothing gated


def test_the_committed_result_is_current() -> None:
    committed = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert structural.run() == committed, "run `uv run python -m evals.structural`"
    assert "not quality judgments" in committed["what"]
    measured = [c for c in committed["components"].values() if c["status"] == "measured"]
    assert len(committed["components"]) == 11
    for c in measured:
        assert c["schema_valid"]["rate"] == 1.0, c["title"]
        assert c["banned_content_free"]["rate"] == 1.0, c["title"]

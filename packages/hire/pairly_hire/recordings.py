# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recorded model responses for the Hire demo drafts (a recordings bundle).

``uv run python -m pairly_hire.recordings`` writes ``pairly_hire/data/recordings.json``;
``--check`` writes nothing and fails when the committed file is out of date (a test
runs it). The ``hire-demo`` seed and the tests import the bundle, so in ``recorded``
mode the drafting routes answer offline for exactly the demo inputs
(``pairly_hire.demo``):

* ``hire_jd_draft/v1`` for each demo brief,
* ``hire_rubric_draft/v1`` for each demo job's drafted title and English description.

The router keys a recording on the exact request (task, prompt version and hash,
provider, model, inputs, schema, settings; ``pairly_core.llm.hashing``), so this mirrors
what ``ModelRouter`` sends for its first choice: provider ``anthropic`` at the prompt's
tier. Each scripted answer is validated against its schema here, and the router
validates it again on replay. If a prompt, a schema or the model catalog changes, run
the command again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from pairly_core.llm.types import TaskSpec
from pairly_hire.demo import DEMO_JOBS
from pairly_hire.drafting import (
    JD_TASK,
    RUBRIC_TASK,
    JobDescriptionDraft,
    jd_inputs,
    rubric_inputs,
)
from pairly_hire.rubric import Rubric

PROVIDER: Final = "anthropic"
PROMPTS_DIR: Final = Path(__file__).resolve().parent / "prompts"
BUNDLE_PATH: Final = Path(__file__).resolve().parent / "data" / "recordings.json"


class RecordingError(RuntimeError):
    """A scripted answer does not fit its schema, or the catalog has no model."""


def entry_for(
    task: TaskSpec, schema: type[BaseModel], inputs: dict[str, Any], answer: BaseModel
) -> dict[str, Any]:
    prompt = PromptRegistry([PROMPTS_DIR]).get(task.name, task.prompt_version)
    if prompt.output_schema != schema.__name__:
        raise RecordingError(f"{task.name}: prompt expects {prompt.output_schema}")
    model = load_catalog(load_prices()).model_for(PROVIDER, prompt.tier)
    if model is None:
        raise RecordingError(f"no {PROVIDER} model for tier {prompt.tier}")
    # Mirrors ModelRouter's request settings: part of the request hash.
    settings = {
        "temperature": prompt.temperature if model.supports_temperature else None,
        "max_output_tokens": prompt.max_output_tokens,
        "effort": model.effort,
        "user_template": USER_TEMPLATE_VERSION,
    }
    clean_inputs = json_round_trip(inputs)
    response = json_round_trip(answer.model_dump(mode="json"))
    schema.model_validate(response)
    s_hash = schema_hash(schema)
    return {
        "request_hash": request_hash(
            task=prompt.task,
            prompt_version=prompt.version,
            prompt_hash=prompt.hash,
            provider=PROVIDER,
            model=model.model,
            inputs=clean_inputs,
            schema_hash=s_hash,
            settings=settings,
        ),
        "task": prompt.task,
        "prompt_version": prompt.version,
        "prompt_hash": prompt.hash,
        "provider": PROVIDER,
        "model": model.model,
        "tier": prompt.tier,
        "schema_name": schema.__name__,
        "schema_hash": s_hash,
        "request": {"inputs": clean_inputs, "settings": settings},
        "response": response,
        "output_complete": True,
        "stop_reason": "end_turn",
        "status": "ok",
    }


def build_bundle() -> str:
    entries: list[dict[str, Any]] = []
    for job in DEMO_JOBS:
        entries.append(
            entry_for(
                JD_TASK,
                JobDescriptionDraft,
                jd_inputs(job.brief),
                job.draft,
            )
        )
        entries.append(
            entry_for(
                RUBRIC_TASK,
                Rubric,
                rubric_inputs(job.draft.title, job.draft.description_en),
                job.rubric,
            )
        )
    entries.sort(key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pairly_hire.recordings")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m pairly_hire.recordings): "
            "packages/hire/pairly_hire/data/recordings.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

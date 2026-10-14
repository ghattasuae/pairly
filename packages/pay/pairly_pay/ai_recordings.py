# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recorded responses for Pay's optional model help (a recordings bundle), so the demo
works offline. **The answers are scripted, not model output.**

``uv run python -m pairly_pay.ai_recordings`` writes ``pairly_pay/data/ai_recordings.json``;
``--check`` writes nothing and fails when the committed file is out of date (a test runs
it). The demo seed imports it next to the extraction bundle (``pairly_pay.recordings``).

* ``pay_coding_suggest/v1``: one entry per demo invoice line under the demo chart of
  accounts and cost centres (``pairly_pay.coding_demo``).
* ``pay_exception_note/v1``: one entry per message template and language
  (``pairly_pay.exceptions.MESSAGE_TEMPLATES``). The inputs name no invoice, vendor or
  person, so these answer for every invoice.

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
import uuid
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.llm.bundle import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    ImportReport,
    dumps_bundle,
    import_recordings,
    parse_bundle,
)
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from pairly_core.llm.types import TaskSpec
from pairly_pay import coding_demo
from pairly_pay.coding import TASK as CODING_TASK
from pairly_pay.coding import ModelCoding, model_inputs
from pairly_pay.exceptions import (
    MESSAGE_TEMPLATES,
    NOTE_TASK,
    ExceptionNote,
    check_note,
    note_inputs,
)
from pairly_pay.models import CodingKind, PayCodingEntry

PROVIDER: Final = "anthropic"
PROMPTS_DIR: Final = Path(__file__).resolve().parent / "prompts"
BUNDLE_PATH: Final = Path(__file__).resolve().parent / "data" / "ai_recordings.json"


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


def _entries(kind: CodingKind, rows: tuple[tuple[str, str, str], ...]) -> list[PayCodingEntry]:
    # The same order and fields as ``pairly_pay.coding.entries_of`` (sorted by code).
    return [
        PayCodingEntry(kind=kind.value, code=code, name=name, name_ar=name_ar)
        for code, name, name_ar in sorted(rows)
    ]


def coding_entries() -> list[dict[str, Any]]:
    gl = _entries(CodingKind.GL_ACCOUNT, coding_demo.GL_ACCOUNTS)
    cc = _entries(CodingKind.COST_CENTRE, coding_demo.COST_CENTRES)
    out: list[dict[str, Any]] = []
    for description, (gl_code, cc_code, reason) in coding_demo.ANSWERS.items():
        answer = ModelCoding(gl_code=gl_code, cost_centre=cc_code, reason=reason)
        out.append(entry_for(CODING_TASK, ModelCoding, model_inputs(description, gl, cc), answer))
    return out


# Scripted personal paragraphs: one for a vendor, one for a colleague, per language.
NOTES: Final[dict[str, dict[str, str]]] = {
    "vendor": {
        "en": "Thank you for your help with this. We want to settle your invoice as soon as "
        "we can.",
        "ar": "شكرا لتعاونكم في هذا الأمر. نرغب في تسوية فاتورتكم في أقرب وقت ممكن.",
    },
    "requester": {
        "en": "Thank you for your help. A quick answer lets us pay the vendor on time.",
        "ar": "شكرا لمساعدتك. الرد السريع يتيح لنا الدفع للمورد في الوقت المحدد.",
    },
}


def note_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for template in MESSAGE_TEMPLATES.values():
        for locale in ("en", "ar"):
            note = NOTES[template.recipient.value][locale]
            problems = check_note(note, locale)
            if problems:
                raise RecordingError(f"{template.id} ({locale}): {problems}")
            out.append(
                entry_for(
                    NOTE_TASK,
                    ExceptionNote,
                    note_inputs(template, locale),
                    ExceptionNote(note=note),
                )
            )
    return out


def build_entries() -> list[dict[str, Any]]:
    entries = coding_entries() + note_entries()
    entries.sort(key=lambda e: e["request_hash"])
    return entries


def build_bundle() -> str:
    text = dumps_bundle(
        {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": build_entries()}
    )
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


async def import_ai_recordings(session: AsyncSession, org_id: uuid.UUID) -> ImportReport:
    """Import the bundle for ``org_id`` (idempotent), in the caller's transaction."""
    return await import_recordings(session, org_id, parse_bundle(recordings_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pairly_pay.ai_recordings")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    data = build_bundle()
    current = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None
    if current == data:
        return 0
    if args.check:
        print(
            "out of date (run: uv run python -m pairly_pay.ai_recordings): "
            "packages/pay/pairly_pay/data/ai_recordings.json",
            file=sys.stderr,
        )
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(data, encoding="utf-8")
    print(f"wrote {BUNDLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

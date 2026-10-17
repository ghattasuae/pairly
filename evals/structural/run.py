# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Run the structural evals (see ``evals/structural/__init__.py``) and write
``evals/results/structural.json`` and the "Structural checks" section of the report."""

from __future__ import annotations

import argparse
import json
import lzma
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

ROOT: Final = Path(__file__).resolve().parents[2]
RESULTS: Final = ROOT / "evals" / "results" / "structural.json"
REPORT: Final = ROOT / "evals" / "report.md"
LIVE: Final = (
    ROOT / "evals" / "recordings" / "hire_live.jsonl.xz",
    ROOT / "evals" / "recordings" / "pay_live.jsonl.xz",
)
MAX_FAILURES: Final = 20
WHAT: Final = (
    "Structural checks of recorded outputs (scripted answers and, where a task has any, live "
    "recordings): schema validity, banned content, em dashes, the component's own "
    "deterministic check, references back to the input, sizes and measured cost. They are "
    "structural checks, not quality judgments: scripted answers are not model output."
)

Check = Callable[[BaseModel, Mapping[str, Any]], list[str]]


# ------------------------------------------------------------------ component checks


def _jd_inclusive(draft: BaseModel, _inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.drafting import JobDescriptionDraft, check_texts

    assert isinstance(draft, JobDescriptionDraft)  # noqa: S101 (the schema of this task)
    checks = check_texts(
        title=draft.title,
        description_en=draft.description_en,
        description_ar=draft.description_ar,
        title_ar=draft.title_ar,
    )
    found = [("en", f) for f in checks.inclusive_en] + [("ar", f) for f in checks.inclusive_ar]
    return sorted({f"inclusive_{lang}:{f.rule}" for lang, f in found})


def _kit(draft: BaseModel, _inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.interviewing.kits import KitItemDraft, check_item

    assert isinstance(draft, KitItemDraft)  # noqa: S101
    return check_item(draft)


def _debrief(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.interviewing.debrief import DebriefDraft, check_draft

    assert isinstance(draft, DebriefDraft)  # noqa: S101
    return check_draft(draft, dict(inputs))


def _debrief_refs(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    known = {str(c.get("id")) for c in inputs.get("criteria") or [] if isinstance(c, Mapping)}
    return [
        f"criteria[{i}]: {c.criterion_id} is not in the request"
        for i, c in enumerate(getattr(draft, "criteria", []))
        if c.criterion_id not in known
    ]


def _reference(draft: BaseModel, _inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.references.summary import ReferenceDraft, check_draft

    assert isinstance(draft, ReferenceDraft)  # noqa: S101
    return check_draft(draft)


def _message(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.messaging.drafting import MessageDraft, check_draft

    assert isinstance(draft, MessageDraft)  # noqa: S101
    return check_draft(draft, dict(inputs))


def _clarify(draft: BaseModel, _inputs: Mapping[str, Any]) -> list[str]:
    from pairly_hire.analytics.calibration import RubricClarification, check_suggestion

    assert isinstance(draft, RubricClarification)  # noqa: S101
    return check_suggestion(draft.suggestion)


def _note(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    from pairly_pay.exceptions import ExceptionNote, check_note

    assert isinstance(draft, ExceptionNote)  # noqa: S101
    return check_note(draft.note, str(inputs.get("language", "en")))


def _codes(inputs: Mapping[str, Any], key: str) -> set[str]:
    return {str(e.get("code")) for e in inputs.get(key) or [] if isinstance(e, Mapping)}


def _coding_accepted(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    """``accept_model_answer``: a GL code of the list shown, or the answer is dropped."""
    gl = str(getattr(draft, "gl_code", "") or "")
    return [] if gl and gl in _codes(inputs, "gl_accounts") else ["gl_code: not in the list shown"]


def _coding_refs(draft: BaseModel, inputs: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    gl = str(getattr(draft, "gl_code", "") or "")
    cc = str(getattr(draft, "cost_centre", "") or "")
    if gl and gl not in _codes(inputs, "gl_accounts"):
        problems.append(f"gl_code {gl} is not in the request")
    if cc and cc not in _codes(inputs, "cost_centres"):
        problems.append(f"cost_centre {cc} is not in the request")
    return problems


# ------------------------------------------------------------------ components


@dataclass(frozen=True, slots=True)
class Component:
    slug: str
    title: str
    task: str
    version: str
    schema: str  # "module:Class"
    sources: tuple[str, ...]
    product_check: tuple[str, Check] | None = None
    references: Check | None = None


COMPONENTS: Final[tuple[Component, ...]] = (
    Component(
        "jd-drafting",
        "Job description drafting",
        "hire_jd_draft",
        "v1",
        "pairly_hire.drafting:JobDescriptionDraft",
        ("packages/hire/pairly_hire/data/recordings.json",),
        ("inclusive_language", _jd_inclusive),
    ),
    Component(
        "rubric-drafting",
        "Rubric drafting",
        "hire_rubric_draft",
        "v1",
        "pairly_hire.rubric:Rubric",
        ("packages/hire/pairly_hire/data/recordings.json",),
    ),
    Component(
        "interview-kits",
        "Interview kits",
        "hire_interview_kit",
        "v1",
        "pairly_hire.interviewing.kits:KitItemDraft",
        (
            "packages/hire/pairly_hire/data/interview_recordings.json",
            "synth/hire/recordings/hire_interviews.json",
        ),
        ("kit_check", _kit),
    ),
    Component(
        "debrief-summary",
        "Debrief summary",
        "hire_debrief_summary",
        "v1",
        "pairly_hire.interviewing.debrief:DebriefDraft",
        ("synth/hire/recordings/hire_interviews.json",),
        ("debrief_check", _debrief),
        _debrief_refs,
    ),
    Component(
        "reference-summary",
        "Reference summary",
        "hire_reference_summary",
        "v1",
        "pairly_hire.references.summary:ReferenceDraft",
        ("synth/hire/recordings/hire_references.json",),
        ("reference_check", _reference),
    ),
    Component(
        "message-personalisation",
        "Message personalisation",
        "hire_message_draft",
        "v1",
        "pairly_hire.messaging.drafting:MessageDraft",
        ("packages/hire/pairly_hire/data/messaging_recordings.json",),
        ("no_invented_facts", _message),
    ),
    Component(
        "injection-span-classifier",
        "Guardrail injection classifier (suspicious spans)",
        "guardrails_injection_classify",
        "v1",
        "pairly_core.guardrails.classifier:InjectionVerdicts",
        (),
    ),
    Component(
        "policy-rag-check",
        "Policy check against documents (RAG)",
        "core_policy_check",
        "v1",
        "pairly_core.policy.rag:PolicyCheckOutput",
        (),
    ),
    Component(
        "gl-coding-suggestion",
        "GL coding suggestion",
        "pay_coding_suggest",
        "v1",
        "pairly_pay.coding:ModelCoding",
        ("packages/pay/pairly_pay/data/ai_recordings.json",),
        ("codes_from_the_lists", _coding_accepted),
        _coding_refs,
    ),
    Component(
        "rubric-clarification",
        "Rubric clarification",
        "hire_rubric_clarify",
        "v1",
        "pairly_hire.analytics.calibration:RubricClarification",
        ("packages/hire/pairly_hire/data/calibration_recordings.json",),
        ("clarification_check", _clarify),
    ),
    Component(
        "exception-note",
        "Exception note",
        "pay_exception_note",
        "v1",
        "pairly_pay.exceptions:ExceptionNote",
        ("packages/pay/pairly_pay/data/ai_recordings.json",),
        ("note_check", _note),
    ),
)


# ------------------------------------------------------------------ reading


def _schema(path: str) -> type[BaseModel]:
    import importlib

    module, name = path.split(":")
    value = getattr(importlib.import_module(module), name)
    if not (isinstance(value, type) and issubclass(value, BaseModel)):
        raise TypeError(f"{path} is not a Pydantic model")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Recorded:
    source: str
    request_hash: str
    schema_hash: str
    inputs: Mapping[str, Any]
    response: Any
    measured: Mapping[str, Any] | None = None


def _bundle_entries(path: str, task: str) -> list[Recorded]:
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    out: list[Recorded] = []
    for e in data.get("entries") or []:
        if e.get("task") != task:
            continue
        request = e.get("request") or {}
        out.append(
            Recorded(
                source=path,
                request_hash=str(e.get("request_hash", "")),
                schema_hash=str(e.get("schema_hash", "")),
                inputs=request.get("inputs") or {},
                response=e.get("response"),
            )
        )
    return out


def _live_entries(task: str) -> list[Recorded]:
    out: list[Recorded] = []
    for path in LIVE:
        if not path.is_file():
            continue
        for line in lzma.decompress(path.read_bytes()).decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            e = row.get("entry") or {}
            if e.get("task") != task:
                continue
            request = e.get("request") or {}
            out.append(
                Recorded(
                    source=str(path.relative_to(ROOT)),
                    request_hash=str(e.get("request_hash", "")),
                    schema_hash=str(e.get("schema_hash", "")),
                    inputs=request.get("inputs") or {},
                    response=e.get("response"),
                    measured=row.get("measured") or {},
                )
            )
    return out


def _provenance(live: int, total: int) -> str | None:
    if not total:
        return None
    if live == total:
        return "live recordings"
    return "scripted answers and live recordings" if live else "scripted answers"


def _rate(passed: int, n: int) -> float | None:
    return round(passed / n, 4) if n else None


def _metric(results: Sequence[tuple[str, list[str]]]) -> dict[str, Any]:
    failed = [(h, p) for h, p in results if p]
    return {
        "rate": _rate(len(results) - len(failed), len(results)),
        "n": len(results),
        "failed": len(failed),
        "failures": [{"request": h[:12], "problems": p[:5]} for h, p in failed[:MAX_FAILURES]],
    }


def _sizes(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "max": None}
    return {"mean": round(sum(values) / len(values), 1), "max": max(values)}


def evaluate(component: Component) -> dict[str, Any]:
    from pairly_core.guardrails.validation import DEFAULT_POLICY, check_banned_content
    from pairly_core.llm.hashing import schema_hash
    from pairly_core.notifications.templating import has_em_dash

    schema = _schema(component.schema)
    seen: dict[str, Recorded] = {}
    for source in component.sources:
        for r in _bundle_entries(source, component.task):
            seen.setdefault(r.request_hash, r)
    live = _live_entries(component.task)
    for r in live:
        seen.setdefault(r.request_hash, r)
    records = list(seen.values())
    prompt = _registry().get(component.task, component.version)
    base: dict[str, Any] = {
        "title": component.title,
        "task": component.task,
        "prompt_version": prompt.version,
        "schema": schema.__name__,
        "sources": list(component.sources) + sorted({r.source for r in live}),
        "recorded_outputs": len(records),
        "provenance": _provenance(len(live), len(records)),
        "prompt_file_chars": len(prompt.system),
    }
    if not records:
        return {
            **base,
            "status": "no_recordings",
            "why": "No committed recording or scripted answer of this task: nothing to check "
            "without a model call.",
        }
    current_hash = schema_hash(schema)
    valid: list[tuple[str, list[str]]] = []
    banned: list[tuple[str, list[str]]] = []
    dashes: list[tuple[str, list[str]]] = []
    product: list[tuple[str, list[str]]] = []
    refs: list[tuple[str, list[str]]] = []
    current: list[tuple[str, list[str]]] = []
    for r in records:
        current.append((r.request_hash, [] if r.schema_hash == current_hash else ["schema_hash"]))
        try:
            value = schema.model_validate(r.response)
        except ValidationError as exc:
            valid.append((r.request_hash, [f"schema:{e['type']}" for e in exc.errors()][:5]))
            continue
        valid.append((r.request_hash, []))
        dumped = value.model_dump(mode="json")
        banned.append(
            (r.request_hash, sorted({i.code for i in check_banned_content(dumped, DEFAULT_POLICY)}))
        )
        dash = [p for p, text in _strings(dumped) if has_em_dash(text)]
        dashes.append((r.request_hash, [f"{p}: an em dash" for p in dash]))
        if component.product_check is not None:
            product.append((r.request_hash, component.product_check[1](value, r.inputs)))
        if component.references is not None:
            refs.append((r.request_hash, component.references(value, r.inputs)))
    measured = [r.measured for r in records if r.measured]
    result: dict[str, Any] = {
        **base,
        "status": "measured",
        "schema_valid": _metric(valid),
        "banned_content_free": _metric(banned),
        "em_dash_free": _metric(dashes),
        "product_check": (
            {"name": component.product_check[0], **_metric(product)}
            if component.product_check is not None
            else None
        ),
        "references_exist": _metric(refs) if component.references is not None else None,
        "schema_current": _metric(current),
        "size": {
            "request_chars": _sizes([len(_canonical(r.inputs)) for r in records]),
            "response_chars": _sizes([len(_canonical(r.response)) for r in records]),
        },
        "cost": (
            {
                "status": "measured",
                "calls": len(measured),
                "input_tokens": sum(int(m.get("input_tokens", 0)) for m in measured),
                "output_tokens": sum(int(m.get("output_tokens", 0)) for m in measured),
                "usd": round(sum(int(m.get("cost_micros", 0)) for m in measured) / 1e6, 6),
            }
            if measured
            else {
                "status": "not_measured",
                "why": "No live recording of this task: scripted answers have no tokens or cost.",
            }
        ),
    }
    return result


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((path or "$", value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            out += _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            out += _strings(item, f"{path}[{index}]")
    return out


def _registry() -> Any:
    from pairly_core.llm.prompt_registry import PromptRegistry, core_prompt_root
    from pairly_core.plugins import discover_plugins, plugin_prompt_roots

    roots = [core_prompt_root(), *plugin_prompt_roots(discover_plugins().values())]
    return PromptRegistry(roots)


def run() -> dict[str, Any]:
    components = {c.slug: evaluate(c) for c in COMPONENTS}
    measured = [c for c in components.values() if c["status"] == "measured"]
    return {
        "what": WHAT,
        "components": components,
        "summary": {
            "components": len(components),
            "measured": len(measured),
            "no_recordings": sorted(k for k, c in components.items() if c["status"] != "measured"),
            "recorded_outputs": sum(c["recorded_outputs"] for c in components.values()),
        },
    }


def main(argv: list[str] | None = None) -> int:
    from evals.structural.report import write_report

    parser = argparse.ArgumentParser(prog="python -m evals.structural")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    result = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_report(args.report, result)
    for slug, c in result["components"].items():
        if c["status"] != "measured":
            print(f"{slug}: no recordings")
            continue
        print(
            f"{slug}: {c['recorded_outputs']} outputs, schema {c['schema_valid']['rate']}, "
            f"banned-free {c['banned_content_free']['rate']}, "
            f"em-dash-free {c['em_dash_free']['rate']}"
            + (
                f", {c['product_check']['name']} {c['product_check']['rate']}"
                if c["product_check"]
                else ""
            )
        )
    print(f"wrote {args.out} and {args.report}")
    return 0

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``extract``: schema-driven extraction with per-field confidence and evidence.

1. The model router runs the ``core_extract`` prompt with the schema description and the
   sanitized document text: a ``GuardedInput`` built from the document's own guardrail
   report (anything else is refused), sent as the guardrails' nonce-delimited data
   block. The model answers each field as text with a verbatim quote
   and a confidence (``ExtractionOutput``).
2. Every answer is checked deterministically: the path must exist in the schema, the
   value must parse as the field's type, the quote must be found in the text the model
   saw and map back to the original document, and the value must be readable from the
   quote. Any failure sets confidence to 0 and flags the field. Nothing is guessed.
3. Fields below their threshold are flagged for correction. The extraction goes to the
   human queue when a required field is missing or flagged, when an optional field has
   a value that is flagged (unless the schema opts out), when the document itself needs
   review (parse warnings, or guardrail findings of medium severity or worse, or a
   guardrail detector that failed), or when the model could not answer (fail closed,
   never partial acceptance).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pairly_core.documents.model import REVIEW_WARNINGS, NormalizedDocument
from pairly_core.extraction.evidence import EvidenceLocator
from pairly_core.extraction.guarded import GuardedInput, UnguardedInputError
from pairly_core.extraction.schema import (
    PATH_RE,
    CompiledSchema,
    GroupSpec,
    LeafSpec,
    Node,
    compile_schema,
)
from pairly_core.extraction.types import (
    ExtractionModel,
    FieldFlag,
    SourceSpan,
    json_value,
)
from pairly_core.extraction.validators import InvalidValue, parse_value, supported_by_quote
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.types import CallContext, RouterResult, TaskSpec

log = structlog.get_logger(__name__)

EXTRACT_TASK = "core_extract"
EXTRACT_PROMPT_VERSION = "v1"
DEFAULT_TASK = TaskSpec(name=EXTRACT_TASK, prompt_version=EXTRACT_PROMPT_VERSION)
MAX_OUTPUT_FIELDS = 2000
MAX_HINTS = 20


# ------------------------------------------------------------------ model output


class FieldAnswer(BaseModel):
    """One field as the model answers it. Everything is text; we parse and check it."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=200)
    value: str | None = Field(default=None, max_length=2000)
    quote: str | None = Field(default=None, max_length=2000)
    confidence: float = Field(ge=0, le=1)


class ExtractionOutput(BaseModel):
    """The ``core_extract`` prompt's output schema (generic over extraction schemas)."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FieldAnswer] = Field(max_length=MAX_OUTPUT_FIELDS)
    # Overall confidence, so a task may set ``TaskSpec.confidence_threshold`` to escalate.
    confidence: float = Field(ge=0, le=1)


# ------------------------------------------------------------------ results


class ExtractionStatus(StrEnum):
    OK = "ok"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True, slots=True)
class FieldResult:
    """One leaf field, flat. ``value`` is typed; ``json_value`` its exact JSON form."""

    path: str
    pattern: str
    kind: str
    value: Any
    raw: str | None
    confidence: float
    threshold: float
    required: bool
    flags: tuple[FieldFlag, ...]
    spans: tuple[SourceSpan, ...]

    @property
    def needs_correction(self) -> bool:
        return bool(self.flags)

    @property
    def json_value(self) -> Any:
        return json_value(self.value)


@dataclass(frozen=True, slots=True)
class ModelCallInfo:
    call_ids: tuple[uuid.UUID, ...]
    chain_id: uuid.UUID | None
    prompt_task: str
    prompt_version: str
    prompt_hash: str | None
    provider: str | None
    model: str | None
    from_recording: bool


@dataclass(frozen=True, slots=True)
class FewShotHint:
    """A reviewer's earlier correction, given to the model as a hint (data, not rules)."""

    field_pattern: str
    old_value: Any
    new_value: Any

    def as_input(self) -> dict[str, Any]:
        return {"field": self.field_pattern, "was": self.old_value, "corrected_to": self.new_value}


@dataclass(frozen=True, slots=True)
class ExtractionResult[S: ExtractionModel]:
    schema: CompiledSchema
    status: ExtractionStatus
    reasons: tuple[str, ...]
    fields: tuple[FieldResult, ...]
    data: S | None
    model_call: ModelCallInfo
    ignored_paths: tuple[str, ...] = ()
    schema_description: Mapping[str, Any] = field(default_factory=dict)

    @property
    def needs_human(self) -> bool:
        return self.status is ExtractionStatus.NEEDS_HUMAN

    def field(self, path: str) -> FieldResult | None:
        for item in self.fields:
            if item.path == path:
                return item
        return None


# ------------------------------------------------------------------ the engine


def _leaf_result(
    leaf: LeafSpec,
    path: str,
    answer: FieldAnswer | None,
    duplicate: bool,
    locator: EvidenceLocator,
    required: bool,
) -> FieldResult:
    flags: list[FieldFlag] = []
    value: Any = None
    raw = answer.value if answer is not None else None
    confidence = 0.0
    spans: tuple[SourceSpan, ...] = ()
    if duplicate:
        flags.append(FieldFlag.DUPLICATE_OUTPUT)
    if answer is not None and raw is not None and raw.strip():
        try:
            value = parse_value(leaf.value, raw)
        except InvalidValue:
            flags.append(FieldFlag.INVALID_VALUE)
        located = locator.locate(answer.quote)
        if located is None:
            flags.append(FieldFlag.NO_EVIDENCE)
        else:
            spans = located.spans
            quote = " ".join(s.quote for s in spans)
            if value is not None and not supported_by_quote(leaf.value, value, quote):
                flags.append(FieldFlag.QUOTE_MISMATCH)
        # No evidence, no credit: confidence counts only for a checked, evidenced value.
        if value is not None and not flags:
            confidence = answer.confidence
            if confidence < leaf.threshold:
                flags.append(FieldFlag.LOW_CONFIDENCE)
    elif required:
        flags.append(FieldFlag.MISSING)
    return FieldResult(
        path=path,
        pattern=leaf.pattern,
        kind=leaf.value.kind.value,
        value=value,
        raw=raw if value is None and raw else None,
        confidence=confidence,
        threshold=leaf.threshold,
        required=required,
        flags=tuple(dict.fromkeys(flags)),
        spans=spans,
    )


@dataclass
class _Walk:
    answers: dict[str, FieldAnswer]
    duplicates: set[str]
    locator: EvidenceLocator
    results: list[FieldResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    route_optional: bool = True

    def note(self, item: FieldResult, required: bool) -> None:
        self.results.append(item)
        if not item.flags:
            return
        answered = item.value is not None or item.raw is not None
        if required or (self.route_optional and answered):
            self.reasons.append(f"{item.flags[0].value}:{item.path}")

    def group_items(self, group: GroupSpec, prefix: str) -> list[int]:
        """Indices of a list group that the model answered, in order, deduplicated."""
        head = f"{prefix}{group.name}["
        seen: set[int] = set()
        for path in self.answers:
            if path.startswith(head):
                index_text = path[len(head) :].split("]", 1)[0]
                if index_text.isdigit() and int(index_text) < group.max_items:
                    seen.add(int(index_text))
        return sorted(seen)


def _walk(
    nodes: Sequence[Node], prefix: str, walk: _Walk, *, parent_required: bool
) -> dict[str, Any]:
    """Build the data dict for one model level, recording field results on the way."""
    data: dict[str, Any] = {}
    for node in nodes:
        if isinstance(node, LeafSpec):
            path = prefix + node.name
            answer = walk.answers.get(path)
            required = node.required and parent_required
            item = _leaf_result(node, path, answer, path in walk.duplicates, walk.locator, required)
            walk.note(item, required)
            data[node.name] = {
                "value": item.value,
                "confidence": item.confidence,
                "spans": list(item.spans),
                "flags": list(item.flags),
                "raw": item.raw,
            }
            continue
        if node.is_list:
            indices = walk.group_items(node, prefix)
            if not indices and node.required and parent_required:
                walk.reasons.append(f"{FieldFlag.MISSING.value}:{prefix}{node.name}")
            items = []
            for position, index in enumerate(indices):
                # Compact the model's indices to 0..n-1 in the stored paths.
                child_prefix = f"{prefix}{node.name}[{position}]."
                original_prefix = f"{prefix}{node.name}[{index}]."
                if position != index:
                    _reindex(walk, original_prefix, child_prefix)
                items.append(_walk(node.fields, child_prefix, walk, parent_required=True))
            data[node.name] = items
        else:
            data[node.name] = _walk(
                node.fields,
                f"{prefix}{node.name}.",
                walk,
                parent_required=parent_required and node.required,
            )
    return data


def _reindex(walk: _Walk, old: str, new: str) -> None:
    for path in [p for p in walk.answers if p.startswith(old)]:
        walk.answers[new + path[len(old) :]] = walk.answers.pop(path)
    for path in [p for p in walk.duplicates if p.startswith(old)]:
        walk.duplicates.discard(path)
        walk.duplicates.add(new + path[len(old) :])


def _index_answers(
    schema: CompiledSchema, answers: Sequence[FieldAnswer]
) -> tuple[dict[str, FieldAnswer], set[str], list[str]]:
    by_path: dict[str, FieldAnswer] = {}
    duplicates: set[str] = set()
    ignored: list[str] = []
    for answer in answers:
        path = answer.path.strip()
        if not PATH_RE.fullmatch(path) or schema.leaf_for(path) is None:
            ignored.append(path[:200])
            continue
        if path in by_path:
            duplicates.add(path)
            continue
        by_path[path] = answer
    return by_path, duplicates, ignored


def _top_leaves(
    nodes: Sequence[Node], prefix: str, parent_required: bool
) -> list[tuple[LeafSpec, str, bool]]:
    """Leaves outside lists, with the effective required flag (every ancestor required)."""
    out: list[tuple[LeafSpec, str, bool]] = []
    for node in nodes:
        if isinstance(node, LeafSpec):
            out.append((node, prefix + node.name, parent_required and node.required))
        elif not node.is_list:
            required = parent_required and node.required
            out.extend(_top_leaves(node.fields, f"{prefix}{node.name}.", required))
    return out


def _failed[S: ExtractionModel](
    schema: CompiledSchema, reason: str, description: Mapping[str, Any], task: TaskSpec
) -> ExtractionResult[S]:
    fields = tuple(
        FieldResult(
            path=path,
            pattern=leaf.pattern,
            kind=leaf.value.kind.value,
            value=None,
            raw=None,
            confidence=0.0,
            threshold=leaf.threshold,
            required=required,
            flags=(FieldFlag.MISSING,) if required else (),
            spans=(),
        )
        for leaf, path, required in _top_leaves(schema.fields, "", True)
    )
    return ExtractionResult(
        schema=schema,
        status=ExtractionStatus.NEEDS_HUMAN,
        reasons=(reason,),
        fields=fields,
        data=None,
        model_call=ModelCallInfo(
            call_ids=(),
            chain_id=None,
            prompt_task=task.name,
            prompt_version=task.prompt_version,
            prompt_hash=None,
            provider=None,
            model=None,
            from_recording=False,
        ),
        schema_description=description,
    )


async def extract[S: ExtractionModel](
    doc: NormalizedDocument,
    sanitized_input: GuardedInput,
    schema: type[S],
    task: TaskSpec = DEFAULT_TASK,
    *,
    router: ModelRouter,
    org_id: uuid.UUID,
    context: CallContext | None = None,
    hints: Sequence[FewShotHint] = (),
) -> ExtractionResult[S]:
    """Extract ``schema`` from ``doc``. Never raises for model failures: those give a
    ``needs_human`` result with no data (fail closed). ``SchemaError`` and ``ValueError``
    are programming errors and do raise; ``UnguardedInputError`` (a ``ValueError``) when
    ``sanitized_input`` is not ``GuardedInput.build(doc, scan_document(doc))`` output for
    this very document."""
    if not isinstance(sanitized_input, GuardedInput):
        raise UnguardedInputError("extract() only accepts a GuardedInput")
    sanitized_input.check(doc)
    compiled = compile_schema(schema)
    description = compiled.describe()
    inputs: dict[str, Any] = {
        "schema": description,
        "document": sanitized_input.data_block,
    }
    if hints:
        inputs["reviewer_corrections"] = [h.as_input() for h in hints[:MAX_HINTS]]
    if not sanitized_input.text.strip():
        return _failed(compiled, "no_text", description, task)
    try:
        result: RouterResult[ExtractionOutput] = await router.complete(
            task=task, inputs=inputs, schema=ExtractionOutput, org_id=org_id, context=context
        )
    except ModelUnavailable as exc:
        log.warning("extraction.model_unavailable", schema=compiled.name, code=exc.code)
        return _failed(compiled, f"model_unavailable:{exc.code}", description, task)

    call_ids = tuple(a.call_id for a in result.attempts if a.call_id is not None)
    return _checked(
        doc,
        sanitized_input,
        schema,
        compiled,
        description,
        task,
        result.value.fields,
        ModelCallInfo(
            call_ids=call_ids or (result.call_id,),
            chain_id=result.chain_id,
            prompt_task=result.task,
            prompt_version=result.prompt_version,
            prompt_hash=result.prompt_hash,
            provider=result.provider,
            model=result.model,
            from_recording=result.from_recording,
        ),
    )


def _checked[S: ExtractionModel](
    doc: NormalizedDocument,
    sanitized_input: GuardedInput,
    schema: type[S],
    compiled: CompiledSchema,
    description: Mapping[str, Any],
    task: TaskSpec,
    answers_in: Sequence[FieldAnswer],
    call: ModelCallInfo,
) -> ExtractionResult[S]:
    """Check every answer against the schema and the document (shared by both sources)."""
    answers, duplicates, ignored = _index_answers(compiled, answers_in)
    walk = _Walk(
        answers=answers,
        duplicates=duplicates,
        locator=EvidenceLocator(doc, sanitized_input),
        route_optional=schema.route_optional_issues,
    )
    data = _walk(compiled.fields, "", walk, parent_required=True)
    reasons = list(walk.reasons)
    review = sorted(set(doc.warning_codes()) & REVIEW_WARNINGS)
    if review:
        reasons.append("document_needs_review:" + ",".join(review))
    reasons.extend(sanitized_input.review_reasons)
    try:
        instance = schema.model_validate(data)
    except ValidationError:  # pragma: no cover - values are parsed to the field types
        log.error("extraction.data_invalid", schema=compiled.name)
        return _failed(compiled, "data_validation_failed", description, task)
    return ExtractionResult(
        schema=compiled,
        status=ExtractionStatus.NEEDS_HUMAN if reasons else ExtractionStatus.OK,
        reasons=tuple(dict.fromkeys(reasons)),
        fields=tuple(walk.results),
        data=instance,
        model_call=call,
        ignored_paths=tuple(ignored),
        schema_description=description,
    )


def extract_from_answers[S: ExtractionModel](
    doc: NormalizedDocument,
    sanitized_input: GuardedInput,
    schema: type[S],
    answers: Sequence[FieldAnswer],
    *,
    source: str,
    source_version: str,
) -> ExtractionResult[S]:
    """Check answers read by a deterministic reader instead of a model (a structured
    e-invoice), with exactly the checks a model's answers get: the path must exist, the
    value must parse, the quote must be found in the sanitized text and back the value.
    ``source`` names the reader (stored as the prompt task, with no model). Raises
    ``UnguardedInputError`` like ``extract``."""
    if not isinstance(sanitized_input, GuardedInput):
        raise UnguardedInputError("extract_from_answers() only accepts a GuardedInput")
    sanitized_input.check(doc)
    compiled = compile_schema(schema)
    description = compiled.describe()
    task = TaskSpec(name=source, prompt_version=source_version)
    if not sanitized_input.text.strip():
        return _failed(compiled, "no_text", description, task)
    return _checked(
        doc,
        sanitized_input,
        schema,
        compiled,
        description,
        task,
        answers,
        ModelCallInfo(
            call_ids=(),
            chain_id=None,
            prompt_task=source,
            prompt_version=source_version,
            prompt_hash=None,
            provider=None,
            model=None,
            from_recording=False,
        ),
    )

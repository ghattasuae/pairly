# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Replay a decision from its record (brief 2.3.4) and store the result.

``replay_decision`` never changes the original record. It:

1. checks the record is intact (``verify_record``: decrypts, content hash, audit chain);
   a record that is not intact fails closed;
2. refuses records of a person's action (``HUMAN_KINDS``): a person cannot be re-run;
3. re-runs every stored model call with the same request, prompt version and model
   settings through ``ModelRouter.replay``: the recorded response by default (exact),
   or the live model when asked. Live replays of a model without temperature 0 are
   flagged ``deterministic = False``;
4. re-evaluates the deterministic part from the stored inputs and the stored policy
   version (``replay_handler``): rule sets (``core.rules``), the risk combiner
   (``core.risk_combiner``), extraction value parsing (``core.extraction``). Workflows
   register more handlers (for example score aggregation) with ``register_handler``;
5. stores a ``decision_replays`` row: ``match``, ``differs`` with a field level diff
   (``pairly_core.llm.diff.json_diff``), or ``failed`` with a reason, and audits it.

Diffs of personal data (extracted values, model output) store a fingerprint of each
side (``sha256:<16 hex>``), never the values. Diffs of public fields (outcomes, scores,
levels) store the values.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.decisions.models import (
    HUMAN_KINDS,
    DecisionKind,
    DecisionRecord,
    DecisionReplay,
    ReplayMode,
    ReplayStatus,
)
from pairly_core.decisions.recorders import (
    HANDLER_EXTRACTION,
    HANDLER_MODELS,
    HANDLER_RISK,
    HANDLER_RULES,
)
from pairly_core.decisions.serialize import EncodingError, decode_typed, fingerprint
from pairly_core.decisions.service import open_sensitive, verify_record
from pairly_core.llm.diff import json_diff
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.store import DatabaseCallStore, StoredCall
from pairly_core.llm.types import DiffEntry, ReplayResult
from pairly_core.policy_versions import PolicyError, get_policy

log = structlog.get_logger("pairly_core.decisions.replay")

DECISION_REPLAYED: Final = "core.decision.replayed"
# Model call statuses that stored an output, so there is something to replay.
_OUTPUT_STATUSES = frozenset({"ok", "invalid_output", "low_confidence"})
MAX_DIFF_ENTRIES: Final = 200


class ReplayRouter(Protocol):
    """What replay needs from the model router (``ModelRouter`` satisfies it)."""

    async def replay(
        self,
        call_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        schema: type[BaseModel],
        live: bool = False,
    ) -> ReplayResult: ...


# ------------------------------------------------------------------ registries

_SCHEMAS: dict[str, type[BaseModel]] = {}


def register_schema(schema: type[BaseModel]) -> None:
    """Make a model output schema replayable (keyed by its class name, as stored)."""
    existing = _SCHEMAS.get(schema.__name__)
    if existing is not None and existing is not schema:
        raise ValueError(f"another schema is registered as {schema.__name__}")
    _SCHEMAS[schema.__name__] = schema


def _core_schemas() -> None:
    from pairly_core.extraction.engine import ExtractionOutput
    from pairly_core.guardrails.classifier import InjectionVerdicts
    from pairly_core.policy.rag import PolicyCheckOutput

    for schema in (ExtractionOutput, PolicyCheckOutput, InjectionVerdicts):
        if schema.__name__ not in _SCHEMAS:
            register_schema(schema)


class ReplayFailedError(Exception):
    """The replay cannot run or cannot be trusted. ``reason`` is stored (fail closed)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason[:128]


@dataclass(frozen=True, slots=True)
class HandlerInput:
    session: AsyncSession
    record: DecisionRecord
    sensitive: Mapping[str, Any]
    # Replayed outputs of the record's model calls, in call order (call id, output).
    model_outputs: Sequence[tuple[uuid.UUID, Any]]


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """``original`` and ``replayed`` are compared with ``json_diff``. ``personal`` marks
    values that must only be stored as fingerprints."""

    original: Any
    replayed: Any
    personal: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


Handler = Callable[[HandlerInput], Awaitable[HandlerResult]]
_HANDLERS: dict[str, Handler] = {}


def register_handler(name: str, handler: Handler) -> None:
    """Register a deterministic re-evaluation (``<namespace>.<name>``)."""
    if name in _HANDLERS and _HANDLERS[name] is not handler:
        raise ValueError(f"replay handler {name!r} is already registered")
    _HANDLERS[name] = handler


# ------------------------------------------------------------------ core handlers


async def _policy_content(session: AsyncSession, record: DecisionRecord, kind: str) -> str:
    """The stored policy version the record applied, after checking its hash."""
    refs = [p for p in record.policy_versions if p.get("kind") == kind]
    if not refs:
        raise ReplayFailedError("policy_version_missing")
    ref = refs[0]
    policy = await get_policy(session, uuid.UUID(str(ref["id"])))
    if policy is None:
        raise ReplayFailedError("policy_version_missing")
    if policy.content_hash != ref.get("hash"):
        raise ReplayFailedError("policy_version_hash_mismatch")
    return policy.content


async def _rules(inp: HandlerInput) -> HandlerResult:
    from pairly_core.policy.rules import RULES_KIND, evaluate_rule_set, parse_rule_set

    content = await _policy_content(inp.session, inp.record, RULES_KIND)
    raw_facts = inp.sensitive.get("facts")
    if raw_facts is None:
        raise ReplayFailedError("facts_not_stored")
    try:
        facts = decode_typed(raw_facts)
        as_of = date.fromisoformat(str(inp.record.inputs["as_of"]))
        rule_set = parse_rule_set(content)
    except (EncodingError, KeyError, ValueError, PolicyError):
        raise ReplayFailedError("stored_inputs_invalid") from None
    evaluation = evaluate_rule_set(rule_set, facts, as_of=as_of)
    original = {
        "overall": inp.record.output.get("overall"),
        "rules": {r["rule_id"]: r["outcome"] for r in inp.record.evidence.get("rule_results", [])},
    }
    replayed = {
        "overall": evaluation.overall.value,
        "rules": {r.rule_id: r.outcome.value for r in evaluation.results},
    }
    return HandlerResult(original, replayed)


async def _risk(inp: HandlerInput) -> HandlerResult:
    from pairly_core.risk.combiner import POLICY_KIND, combine, parse_risk_policy
    from pairly_core.risk.types import RiskSignal

    policy = None
    if inp.record.policy_versions:
        content = await _policy_content(inp.session, inp.record, POLICY_KIND)
        try:
            policy = parse_risk_policy(content)
        except PolicyError:
            raise ReplayFailedError("stored_policy_invalid") from None
    try:
        signals = [
            RiskSignal.model_validate(
                {
                    "detector": s["detector"],
                    "version": s["version"],
                    "status": s["status"],
                    "score": s["score"],
                    "label": s["label"],
                    "severity": s["severity"],
                    "evidence": s["evidence"],
                    "explanation_key": s["explanation_key"],
                }
            )
            for s in inp.record.evidence.get("risk_signals", [])
        ]
    except (KeyError, TypeError, ValidationError):
        raise ReplayFailedError("stored_inputs_invalid") from None
    combined = combine(signals, policy)
    stored_reasons = list(inp.record.output.get("reasons", []))
    replayed_reasons = list(combined.reasons)
    # The service adds a leading reason when the policy row was unusable.
    invalid_policy = stored_reasons[:1] == ["risk_policy_invalid"]
    if invalid_policy:
        replayed_reasons = ["risk_policy_invalid", *replayed_reasons]
    original = {
        "score": inp.record.output.get("score"),
        "level": inp.record.output.get("level"),
        "requires_human": inp.record.output.get("requires_human"),
        "reasons": stored_reasons,
    }
    replayed = {
        "score": combined.score,
        "level": combined.level.value,
        "requires_human": combined.requires_human or invalid_policy,
        "reasons": replayed_reasons,
    }
    return HandlerResult(original, replayed)


async def _extraction(inp: HandlerInput) -> HandlerResult:
    from pairly_core.extraction.engine import ExtractionOutput
    from pairly_core.extraction.models import Extraction
    from pairly_core.extraction.schema import StoredSpecs
    from pairly_core.extraction.types import json_value
    from pairly_core.extraction.validators import InvalidValue, parse_value

    try:
        extraction_id = uuid.UUID(str(inp.record.inputs["extraction_id"]))
    except (KeyError, ValueError):
        raise ReplayFailedError("stored_inputs_invalid") from None
    row = await inp.session.scalar(select(Extraction).where(Extraction.id == extraction_id))
    if row is None:
        raise ReplayFailedError("extraction_missing")
    accepted = [out for _, out in inp.model_outputs if isinstance(out, dict)]
    if not accepted:
        raise ReplayFailedError("no_model_output")
    try:
        output = ExtractionOutput.model_validate(accepted[-1])
    except ValidationError:
        raise ReplayFailedError("model_output_invalid") from None
    specs = StoredSpecs.from_json(row.field_specs)
    answers: dict[str, str | None] = {}
    for answer in output.fields:
        answers.setdefault(answer.path.strip(), answer.value)
    stored_values = dict(inp.sensitive.get("fields", {}))
    replayed: dict[str, Any] = {}
    for path in stored_values:
        text = answers.get(path)
        resolved = specs.value_spec(path)
        value: Any = None
        if text is not None and text.strip() and resolved is not None:
            try:
                value = json_value(parse_value(resolved[1], text))
            except InvalidValue:
                value = None
        replayed[path] = value
    # A value the engine dropped (no evidence, invalid) is None in the record too.
    original = dict(stored_values)
    replayed = {p: (replayed[p] if original[p] is not None else None) for p in original}
    return HandlerResult(original, replayed, personal=True)


def _install_core_handlers() -> None:
    for name, handler in (
        (HANDLER_RULES, _rules),
        (HANDLER_RISK, _risk),
        (HANDLER_EXTRACTION, _extraction),
    ):
        if name not in _HANDLERS:
            register_handler(name, handler)


# ------------------------------------------------------------------ the replay


@dataclass
class ReplayOutcome:
    replay_id: uuid.UUID
    decision_id: str
    mode: ReplayMode
    deterministic: bool
    status: ReplayStatus
    reason: str | None
    diff: list[dict[str, Any]]
    details: dict[str, Any]


def _entries(prefix: str, diff: Sequence[DiffEntry], *, personal: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in diff:
        path = prefix + entry.path[1:] if entry.path.startswith("$") else prefix + entry.path
        if personal:
            out.append(
                {
                    "path": path,
                    "original": fingerprint(entry.original),
                    "replayed": fingerprint(entry.replayed),
                    "fingerprinted": True,
                }
            )
        else:
            out.append({"path": path, "original": entry.original, "replayed": entry.replayed})
    return out


def _temperature_zero(call: StoredCall) -> bool:
    settings = call.request.get("settings", {}) if isinstance(call.request, Mapping) else {}
    return isinstance(settings, Mapping) and settings.get("temperature") == 0


async def _replay_models(
    session: AsyncSession,
    router: ReplayRouter | None,
    record: DecisionRecord,
    *,
    live: bool,
) -> tuple[list[tuple[uuid.UUID, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Replay every stored call. Returns outputs, diff entries, per-call details and
    whether the run was deterministic."""
    outputs: list[tuple[uuid.UUID, Any]] = []
    diff: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    deterministic = True
    if not record.model_call_ids:
        return outputs, diff, details, deterministic
    if router is None:
        raise ReplayFailedError("model_router_unavailable")
    _core_schemas()
    store = DatabaseCallStore()
    for call_id in record.model_call_ids:
        call = await store.get(session, call_id)
        if call is None:
            raise ReplayFailedError("model_call_missing")
        if call.status not in _OUTPUT_STATUSES:
            details.append({"call_id": str(call_id), "skipped": "no_output_stored"})
            continue
        schema = _SCHEMAS.get(call.schema_name)
        if schema is None:
            raise ReplayFailedError(f"schema_not_registered:{call.schema_name}")
        if live and not _temperature_zero(call):
            deterministic = False
        try:
            result = await router.replay(call_id, org_id=record.org_id, schema=schema, live=live)
        except ModelUnavailable as exc:
            raise ReplayFailedError(f"model_replay_unavailable:{exc.code}") from None
        except ValueError:
            raise ReplayFailedError("model_call_not_replayable") from None
        # Same output and same validation outcome as the original attempt.
        status_same = result.replayed_status == call.status or (
            # The task's confidence threshold is not stored with the call, so a replay
            # cannot re-apply it; the output itself is still compared.
            call.status == "low_confidence" and result.replayed_status == "ok"
        )
        entries = _entries(f"model[{call_id}]", result.diff, personal=True)
        if not status_same:
            entries.append(
                {
                    "path": f"model[{call_id}].status",
                    "original": call.status,
                    "replayed": result.replayed_status,
                }
            )
        diff.extend(entries)
        details.append(
            {
                "call_id": str(call_id),
                "replay_call_id": str(result.replay_call_id) if result.replay_call_id else None,
                "source": result.source,
                "prompt_unchanged": result.prompt_unchanged,
                "schema_unchanged": result.schema_unchanged,
                "differences": len(entries),
            }
        )
        if not result.prompt_unchanged:
            raise ReplayFailedError("prompt_version_changed")
        if not result.schema_unchanged:
            raise ReplayFailedError("output_schema_changed")
        if call.status == "ok":
            outputs.append((call_id, result.replayed_output))
    return outputs, diff, details, deterministic


async def _run(
    session: AsyncSession,
    router: ReplayRouter | None,
    record: DecisionRecord,
    *,
    live: bool,
) -> tuple[ReplayStatus, str | None, list[dict[str, Any]], dict[str, Any], bool]:
    problem = await verify_record(session, record)
    if problem is not None:
        raise ReplayFailedError(f"record_not_intact:{problem}")
    if DecisionKind(record.kind) in HUMAN_KINDS:
        raise ReplayFailedError("human_decision_not_replayable")
    sensitive = open_sensitive(record) or {}
    if not isinstance(sensitive, Mapping):
        raise ReplayFailedError("stored_inputs_invalid")
    outputs, diff, calls, deterministic = await _replay_models(session, router, record, live=live)
    details: dict[str, Any] = {"model_calls": calls}
    handler_name = record.replay_handler
    if handler_name is not None and handler_name != HANDLER_MODELS:
        _install_core_handlers()
        handler = _HANDLERS.get(handler_name)
        if handler is None:
            raise ReplayFailedError(f"handler_not_registered:{handler_name}")
        result = await handler(HandlerInput(session, record, sensitive, outputs))
        recomputed = json_diff(result.original, result.replayed)
        diff.extend(_entries("recomputed", recomputed, personal=result.personal))
        details["handler"] = handler_name
        details["recomputed_differences"] = len(recomputed)
        details.update(dict(result.details))
    elif not record.model_call_ids:
        raise ReplayFailedError("nothing_to_replay")
    status = ReplayStatus.DIFFERS if diff else ReplayStatus.MATCH
    return status, None, diff[:MAX_DIFF_ENTRIES], details, deterministic


async def replay_decision(
    session: AsyncSession,
    router: ReplayRouter | None,
    record: DecisionRecord,
    *,
    live: bool,
    actor_type: ActorType,
    actor_id: str,
) -> ReplayOutcome:
    """Replay ``record``, store the result and audit it, in the session's transaction."""
    mode = ReplayMode.LIVE if live else ReplayMode.RECORDED
    try:
        status, reason, diff, details, deterministic = await _run(
            session, router, record, live=live
        )
    except ReplayFailedError as exc:
        status, reason, diff, details, deterministic = (
            ReplayStatus.FAILED,
            exc.reason,
            [],
            {},
            not live,
        )
    except Exception as exc:  # fail closed: an unexpected error is a failed replay
        log.error("decision_replay_error", decision_id=record.id, error=type(exc).__name__)
        status, reason, diff, details, deterministic = (
            ReplayStatus.FAILED,
            f"replay_error:{type(exc).__name__}",
            [],
            {},
            not live,
        )
    replay_id = uuid.uuid4()
    await session.execute(
        pg_insert(DecisionReplay).values(
            id=replay_id,
            org_id=record.org_id,
            decision_id=record.id,
            mode=mode.value,
            deterministic=deterministic,
            status=status.value,
            reason=reason,
            diff=diff,
            details=details,
            requested_by_type=actor_type.value,
            requested_by_id=actor_id,
        )
    )
    await append_audit(
        session,
        action=DECISION_REPLAYED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="decision_record",
        entity_id=record.id,
        workflow=record.workflow,
        payload={
            "replay_id": str(replay_id),
            "mode": mode.value,
            "status": status.value,
            "reason": reason,
            "deterministic": deterministic,
            "differences": len(diff),
        },
    )
    return ReplayOutcome(
        replay_id=replay_id,
        decision_id=record.id,
        mode=mode,
        deterministic=deterministic,
        status=status,
        reason=reason,
        diff=diff,
        details=details,
    )


async def list_replays(session: AsyncSession, decision_id: str) -> list[DecisionReplay]:
    rows = await session.scalars(
        select(DecisionReplay)
        .where(DecisionReplay.decision_id == decision_id)
        .order_by(DecisionReplay.created_at.desc())
        .limit(50)
    )
    return list(rows)

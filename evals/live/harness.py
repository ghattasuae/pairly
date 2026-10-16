# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The live eval harness: the production ``ModelRouter`` with the real Anthropic
provider, an in-memory call store backed by a recordings file, and a hard cost cap.

* Routing is production's: each prompt starts at its tier (small) and escalates to the
  next tier on invalid output or low confidence (``ModelRouter``, unchanged). Only one
  thing is added (``LiveRouter``): before a live attempt, a recording of the same
  request for the same document (``scope``) is replayed instead, so a second run of the
  same suite costs nothing.
* Every live attempt that returns a model output is appended to the recordings file
  (JSON lines) at once, with what it measured: tokens, cost, latency, model. A crash or
  the cost cap never loses a paid response. At the end of the run the file is folded
  into the committed, xz-compressed ``<product>_live.jsonl.xz``.
* Response reuse (the production router's, on by default): a request already
  answered for another document reuses the first recorded answer (``reused_from``, no
  cost), so byte-identical model input (a fairness pair) gets identical output, as in
  production. ``PAIRLY_LLM_REUSE_RESPONSES=false`` measures without it.
* ``CapBudget`` is the router's budget: before each request the router reserves the
  worst-case cost, and the reservation is refused once the run's spend plus that worst
  case would pass ``--max-cost-usd``. The run then stops after the current document,
  which is dropped from the measurement, and the result is marked partial.
* The API key is read only by ``load_llm_settings`` (from the environment or ``.env``)
  and handed to the SDK. Nothing here prints, logs or stores it.
"""

from __future__ import annotations

import json
import lzma
import math
import os
import statistics
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import TenantSession
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.errors import BudgetExceeded
from pairly_core.llm.hashing import request_hash
from pairly_core.llm.prompt_registry import PromptRegistry, Root, core_prompt_root
from pairly_core.llm.providers.anthropic import AnthropicProvider
from pairly_core.llm.providers.base import ModelProvider, ProviderRequest, ProviderResponse
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode, load_llm_settings
from pairly_core.llm.store import OUTPUT_STATUSES, StoredCall

ROOT: Final = Path(__file__).resolve().parents[2]
ENV_FILE: Final = ROOT / ".env"
RECORDINGS_DIR: Final = ROOT / "evals" / "recordings"
RESULTS_DIR: Final = ROOT / "evals" / "results"
RECORDING_FORMAT: Final = "pairly.live-eval-recording"
ORG: Final = uuid.UUID(int=0)
_OUTPUT: Final = frozenset(s.value for s in OUTPUT_STATUSES)
_ENTRY_FIELDS: Final = (
    "request_hash",
    "task",
    "prompt_version",
    "prompt_hash",
    "provider",
    "model",
    "tier",
    "schema_name",
    "schema_hash",
    "request",
    "response",
    "output_complete",
    "stop_reason",
    "status",
)
_MEASURED: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_micros",
    "latency_ms",
    "retries",
)


# ------------------------------------------------------------------ recordings files
#
# Committed recordings are xz-compressed (``<product>_live.jsonl.xz``); a run appends
# each new response to the plain ``<product>_live.jsonl`` next to it at once (crash
# safe), and ``compress_recordings`` folds that file into the compressed one at the
# end of the run. Both forms are read.

XZ_SUFFIX: Final = ".xz"


def recording_files(path: Path) -> tuple[Path, Path]:
    """(plain, compressed) for a recordings path given in either form."""
    if path.suffix == XZ_SUFFIX:
        return path.with_suffix(""), path
    return path, path.with_name(path.name + XZ_SUFFIX)


def read_recording_lines(path: Path) -> list[str]:
    """Every non-blank line of the compressed then the plain recordings file, once
    each, in order."""
    plain, compressed = recording_files(path)
    lines: list[str] = []
    if compressed.is_file():
        lines += lzma.decompress(compressed.read_bytes()).decode("utf-8").splitlines()
    if plain.is_file():
        lines += plain.read_text(encoding="utf-8").splitlines()
    return [line for line in dict.fromkeys(lines) if line.strip()]


def compress_recordings(path: Path) -> Path | None:
    """Fold the plain recordings file into the compressed one (deterministic xz: same
    lines, same bytes) and remove the plain file. Returns the compressed file, or
    ``None`` when there is nothing to compress."""
    plain, compressed = recording_files(path)
    if not plain.is_file():
        return None
    lines = read_recording_lines(path)
    data = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    tmp = compressed.with_name(compressed.name + ".tmp")
    tmp.write_bytes(lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=9))
    tmp.replace(compressed)
    plain.unlink()
    return compressed


class LiveSetupError(RuntimeError):
    """The live run cannot start (mode not live, no key)."""


def live_settings() -> LLMSettings:
    """Settings from the environment, then ``.env`` (the environment wins). The process
    must run with ``PAIRLY_MODEL_MODE=live``: ``.env`` keeps ``recorded``."""
    settings = load_llm_settings(_env_file=ENV_FILE if ENV_FILE.is_file() else None)
    if settings.model_mode is not ModelMode.LIVE:
        raise LiveSetupError("set PAIRLY_MODEL_MODE=live in the environment of this run")
    if settings.anthropic_api_key is None:
        raise LiveSetupError("no Anthropic API key configured")
    return settings


# ------------------------------------------------------------------ budget


@dataclass
class CapBudget:
    """The router's budget for one run: refuses a reservation that could pass the cap."""

    limit_micros: int
    spent: int = 0
    reserved: dict[uuid.UUID, int] = field(default_factory=dict)
    exhausted: bool = False

    async def reserve(
        self, session: AsyncSession, org_id: uuid.UUID, amount_micros: int
    ) -> uuid.UUID:
        if self.spent + sum(self.reserved.values()) + amount_micros > self.limit_micros:
            self.exhausted = True
            raise BudgetExceeded("the live eval cost cap is reached")
        rid = uuid.uuid4()
        self.reserved[rid] = amount_micros
        return rid

    async def release(
        self, session: AsyncSession, org_id: uuid.UUID, reservation_id: uuid.UUID
    ) -> None:
        self.reserved.pop(reservation_id, None)

    async def settle(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        reservation_id: uuid.UUID | None,
        cost_micros: int,
    ) -> None:
        if reservation_id is not None:
            self.reserved.pop(reservation_id, None)
        self.spent += cost_micros


# ------------------------------------------------------------------ store and recordings


def _check_entry(entry: Mapping[str, Any]) -> bool:
    """The entry's request hash matches its own fields (an edited line never answers)."""
    try:
        request = entry["request"]
        return bool(
            request_hash(
                task=entry["task"],
                prompt_version=entry["prompt_version"],
                prompt_hash=entry["prompt_hash"],
                provider=entry["provider"],
                model=entry["model"],
                inputs=request["inputs"],
                schema_hash=entry["schema_hash"],
                settings=request["settings"],
            )
            == entry["request_hash"]
        )
    except (KeyError, TypeError, ValueError):
        return False


@dataclass
class LiveCallStore:
    """In-memory ``CallStore``. Recordings are looked up per (scope, request hash)."""

    path: Path
    scope: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    recordings: dict[tuple[str, str], StoredCall] = field(default_factory=dict)
    # The first recording of each request hash, any document (live mode reuse).
    first: dict[str, tuple[str, StoredCall]] = field(default_factory=dict)
    measured: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    loaded: int = 0
    refused: int = 0

    def __post_init__(self) -> None:
        # Responses are appended to the plain file; the compressed one is read only.
        self.path = recording_files(self.path)[0]

    def load(self) -> None:
        for line in read_recording_lines(self.path):
            item = json.loads(line)
            entry = item.get("entry", {})
            if item.get("format") != RECORDING_FORMAT or not _check_entry(entry):
                self.refused += 1
                continue
            stored = _stored(uuid.uuid4(), entry)
            self.recordings[(str(item["scope"]), entry["request_hash"])] = stored
            self.first.setdefault(entry["request_hash"], (str(item["scope"]), stored))
            self.measured[stored.id] = {
                "model_returned": item.get("model_returned"),
                **{k: int(item["measured"].get(k, 0)) for k in _MEASURED},
            }
            self.loaded += 1

    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
        call_id = values.setdefault("id", uuid.uuid4())
        assert isinstance(call_id, uuid.UUID)  # noqa: S101
        row = {**values, "scope": self.scope}
        self.rows.append(row)
        if (
            row["mode"] == ModelMode.LIVE.value
            and row["status"] in _OUTPUT
            and row.get("reused_from") is None
        ):
            self._record(row)
        return call_id

    def _record(self, row: dict[str, Any]) -> None:
        entry = {name: row.get(name) for name in _ENTRY_FIELDS}
        entry.setdefault("output_complete", True)
        item = {
            "format": RECORDING_FORMAT,
            "scope": row["scope"],
            "entry": entry,
            "measured": {k: int(row.get(k) or 0) for k in _MEASURED},
            "model_returned": row.get("model_returned"),
        }
        stored = _stored(row["id"], entry)
        self.recordings[(str(row["scope"]), str(entry["request_hash"]))] = stored
        self.first.setdefault(str(entry["request_hash"]), (str(row["scope"]), stored))
        self.measured[stored.id] = {"model_returned": row.get("model_returned"), **item["measured"]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def find_scoped(self, request_hash_: str) -> StoredCall | None:
        return self.recordings.get((self.scope, request_hash_))

    async def find_recording(self, session: AsyncSession, request_hash: str) -> StoredCall | None:
        return self.find_scoped(request_hash)

    def first_recording(self, request_hash: str) -> tuple[str, StoredCall] | None:
        """The first recorded answer to ``request_hash`` (any document) and its scope."""
        return self.first.get(request_hash)

    async def find_reusable(
        self, session: AsyncSession, request_hash: str, *, org_id: uuid.UUID, since: Any
    ) -> StoredCall | None:
        """Live mode reuse: the first recorded answer to ``request_hash``, whichever
        document it was recorded for (one organisation, ``ORG``). Recordings carry no
        date, so the TTL does not apply here."""
        found = self.first.get(request_hash)
        return found[1] if found is not None else None

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None:
        for row in self.rows:
            if row["id"] == call_id:
                return _stored(call_id, row)
        return None


def _stored(call_id: uuid.UUID, entry: Mapping[str, Any]) -> StoredCall:
    return StoredCall(
        id=call_id,
        chain_id=uuid.uuid4(),
        task=entry["task"],
        prompt_version=entry["prompt_version"],
        prompt_hash=entry["prompt_hash"],
        provider=entry["provider"],
        model=entry["model"],
        tier=entry["tier"],
        schema_name=entry["schema_name"],
        schema_hash=entry["schema_hash"],
        request_hash=entry["request_hash"],
        mode=ModelMode.LIVE.value,
        status=entry["status"],
        request=dict(entry["request"]),
        response=entry.get("response"),
        output_complete=bool(entry.get("output_complete", True)),
        stop_reason=entry.get("stop_reason"),
        confidence=entry.get("confidence"),
    )


# ------------------------------------------------------------------ router and provider


class ModelIdProvider:
    """Wraps a provider to note the model id the API reports for each response."""

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner
        self.last_model: str | None = None

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def available(self) -> bool:
        return self._inner.available

    async def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.last_model = None
        response = await self._inner.generate(request)
        self.last_model = response.model
        return response


class LiveRouter(ModelRouter):
    """``ModelRouter`` in live mode that replays a per-document recording first.

    With response reuse on (``PAIRLY_LLM_REUSE_RESPONSES``, the production default), a
    request already answered for another document reuses that answer, exactly as the
    production router reuses an organisation's earlier answer: no call, no cost, the
    row names the call (``reused_from``). The document's own recording still replays
    as a replay (its recorded cost and latency). ``allow_reuse`` turns reuse off for
    one stretch of a run (repeat sampling)."""

    store: LiveCallStore
    anthropic: ModelIdProvider
    allow_reuse: bool = True

    async def _attempt_reuse(self, **kwargs: Any) -> Any:
        if not self.allow_reuse:
            return None
        found = self.store.first_recording(kwargs["candidate"].request_hash)
        if found is None or found[0] == self.store.scope:
            return None  # nothing to reuse, or this document's own answer (a replay)
        return await super()._attempt_reuse(**kwargs)

    async def _attempt_live(self, **kwargs: Any) -> Any:
        candidate = kwargs["candidate"]
        recording = self.store.find_scoped(candidate.request_hash)
        if recording is not None:
            return await self._replay_recording(
                kwargs["org_id"],
                recording,
                kwargs["row"],
                kwargs["schema"],
                kwargs["task"],
                kwargs["inputs"],
            )
        row = kwargs["row"]

        def tagged(**values: Any) -> dict[str, Any]:
            # The model id the API answered with (None when it did not answer).
            return {**row(**values), "model_returned": self.anthropic.last_model}

        # ``model_returned`` is not a ``model_calls`` column: it only lives on the
        # in-memory row and in the recordings file.
        kwargs["row"] = tagged
        return await super()._attempt_live(**kwargs)


def build_live_router(
    settings: LLMSettings,
    *,
    recordings: Path,
    max_cost_usd: float,
    prompt_roots: Iterable[Root] = (),
    provider: ModelProvider | None = None,
) -> tuple[LiveRouter, LiveCallStore, CapBudget]:
    """``provider`` replaces the real Anthropic provider (tests only)."""
    prices = load_prices(settings.llm_prices_file)
    catalog = load_catalog(prices, settings.llm_models_file)
    if provider is None:
        key = settings.anthropic_api_key
        provider = AnthropicProvider(
            key.get_secret_value() if key is not None else None,
            timeout_seconds=settings.llm_request_timeout_seconds,
        )
    wrapped = ModelIdProvider(provider)
    store = LiveCallStore(recordings)
    store.load()
    budget = CapBudget(limit_micros=math.floor(max_cost_usd * 1_000_000))
    router = LiveRouter(
        settings=settings,
        # Only Anthropic: the OpenAI fallback has no key in this run.
        providers={"anthropic": wrapped},
        catalog=catalog,
        prices=prices,
        prompts=PromptRegistry([*prompt_roots, core_prompt_root()]),
        sessionmaker=async_sessionmaker(sync_session_class=TenantSession),
        budget=budget,
        store=store,
    )
    router.store = store
    router.anthropic = wrapped
    return router, store, budget


# ------------------------------------------------------------------ measuring


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (no interpolation): a value that was measured."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "mean_ms": round(statistics.mean(values), 1) if values else None,
    }


@dataclass
class Meter:
    """Running spend and per-document accounting of the store's rows."""

    store: LiveCallStore
    budget: CapBudget
    every: int
    started: float = field(default_factory=time.monotonic)
    docs: int = 0
    _seen: int = 0
    per_doc: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cut_calls: list[dict[str, Any]] = field(default_factory=list)

    def begin(self, scope: str) -> None:
        self.store.scope = scope
        self._seen = len(self.store.rows)

    def end(self, scope: str) -> list[dict[str, Any]]:
        calls = [self._call(r) for r in self.store.rows[self._seen :]]
        self.per_doc[scope] = calls
        self.docs += 1
        if self.every and self.docs % self.every == 0:
            self.print_progress()
        return calls

    def drop(self, scope: str) -> None:
        """A document the cap cut short: out of the measurement, its spend kept apart."""
        self.cut_calls += self.per_doc.pop(scope, [])

    def print_progress(self) -> None:
        print(
            f"[live] {self.docs} documents, spent {self.budget.spent / 1e6:.4f} USD "
            f"(cap {self.budget.limit_micros / 1e6:.2f}), "
            f"{time.monotonic() - self.started:.0f}s",
            flush=True,
        )

    def _call(self, row: Mapping[str, Any]) -> dict[str, Any]:
        replayed = bool(row.get("from_recording"))
        reused = row.get("reused_from") is not None
        source = self.store.measured.get(row["source_call_id"]) if replayed else None
        # A reused answer costs nothing and takes no model time, as in production.
        measured = source or {k: 0 if reused else int(row.get(k) or 0) for k in _MEASURED}
        return {
            "reused": reused,
            "task": row["task"],
            "tier": row["tier"],
            "model": row["model"],
            "model_returned": (source or {}).get("model_returned")
            if replayed
            else row.get("model_returned"),
            "status": row["status"],
            "error_kind": row.get("error_kind"),
            "replayed": replayed,
            "spent_micros": 0 if replayed else int(row.get("cost_micros") or 0),
            **{k: measured.get(k, 0) for k in _MEASURED},
        }

    def summary(self, scopes: Iterable[str] | None = None) -> dict[str, Any]:
        chosen = list(self.per_doc) if scopes is None else [s for s in scopes if s in self.per_doc]
        calls = [c for s in chosen for c in self.per_doc[s]]
        answered = [c for c in calls if c["status"] in _OUTPUT]
        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in calls:
            by_task[c["task"]].append(c)
        doc_latency = [
            float(sum(c["latency_ms"] for c in self.per_doc[s])) for s in chosen if self.per_doc[s]
        ]
        doc_cost = [sum(c["cost_micros"] for c in self.per_doc[s]) / 1e6 for s in chosen]
        return {
            "documents": len(chosen),
            "calls": len(calls),
            "calls_live_this_run": sum(1 for c in calls if not c["replayed"] and not c["reused"]),
            "calls_replayed_from_recordings": sum(1 for c in calls if c["replayed"]),
            "calls_reused": sum(1 for c in calls if c["reused"]),
            "models": dict(Counter(c["model"] for c in answered)),
            "models_returned_by_api": dict(
                Counter(str(c["model_returned"]) for c in answered if c["model_returned"])
            ),
            "status": dict(Counter(c["status"] for c in calls)),
            "provider_errors": dict(
                Counter(str(c["error_kind"]) for c in calls if c["status"] == "provider_error")
            ),
            "escalated_calls": sum(1 for c in calls if c["tier"] != "small"),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost_usd_measured": round(sum(c["cost_micros"] for c in calls) / 1e6, 6),
            "cost_usd_spent_this_run": round(sum(c["spent_micros"] for c in calls) / 1e6, 6),
            "cost_usd_spent_on_documents_cut_by_cap": round(
                sum(c["spent_micros"] for c in self.cut_calls) / 1e6, 6
            ),
            "cost_usd_per_document": {
                "mean": round(statistics.mean(doc_cost), 6) if doc_cost else None,
                "p50": percentile(doc_cost, 0.5),
                "p95": percentile(doc_cost, 0.95),
            },
            "latency_per_call": latency_summary([float(c["latency_ms"]) for c in answered]),
            "latency_per_document": latency_summary(doc_latency),
            "per_task": {
                task: {
                    "calls": len(items),
                    "models": dict(Counter(c["model"] for c in items)),
                    "input_tokens": sum(c["input_tokens"] for c in items),
                    "output_tokens": sum(c["output_tokens"] for c in items),
                    "cost_usd_measured": round(sum(c["cost_micros"] for c in items) / 1e6, 6),
                    "latency": latency_summary(
                        [float(c["latency_ms"]) for c in items if c["status"] in _OUTPUT]
                    ),
                }
                for task, items in sorted(by_task.items())
            },
            "latency_note": (
                "Model call latency as measured when the call was made live (a replayed "
                "call reports its recording's latency). Per document: the sum of its calls, "
                "made one after the other."
            ),
        }


DocRunner = Callable[[Any], Coroutine[Any, Any, Any]]


async def run_docs(
    docs: Sequence[Any],
    meter: Meter,
    budget: CapBudget,
    one: DocRunner,
    *,
    scope: Callable[[Any], str] | None = None,
) -> tuple[dict[str, Any], list[str], list[str], bool]:
    """Run ``one`` per document until done or the cap is reached. Returns the outputs by
    scope (the document id unless ``scope`` names it, e.g. a repeat run), the errors,
    the scopes cut by the cap and whether it stopped early."""
    outputs: dict[str, Any] = {}
    errors: list[str] = []
    cut: list[str] = []
    for spec in docs:
        if budget.exhausted:
            break
        doc_id = scope(spec) if scope is not None else spec.record.id
        meter.begin(doc_id)
        try:
            out = await one(spec)
        except Exception as exc:  # recorded, never hidden
            meter.end(doc_id)
            if budget.exhausted:
                meter.drop(doc_id)
                cut.append(doc_id)
                break
            errors.append(f"{doc_id}: {type(exc).__name__}")
            continue
        meter.end(doc_id)
        if budget.exhausted:
            # The cap refused a call of this document: it is not fully measured.
            meter.drop(doc_id)
            cut.append(doc_id)
            break
        outputs[doc_id] = out
    if not meter.every or meter.docs % meter.every:
        meter.print_progress()
    return outputs, errors, cut, budget.exhausted


def run_status(*, stopped_by_cap: bool, measured: int, planned: int, errors: int) -> str:
    if stopped_by_cap or measured < planned or errors:
        return "partial"
    return "complete"


def merge_result(path: Path, key: str, value: Any) -> None:
    """Write ``value`` under ``key`` of the JSON file at ``path`` (other keys kept)."""
    current: dict[str, Any] = {}
    if path.is_file():
        current = json.loads(path.read_text(encoding="utf-8"))
    current[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)

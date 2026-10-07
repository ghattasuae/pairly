# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Versioned prompt files.

Layout: ``<root>/<task>/<version>.md``. Each file starts with YAML front matter:

    ---
    task: core_smoke            # must equal the directory name
    version: v1                 # must equal the file name
    tier: small                 # starting tier (small or large)
    temperature: 0              # must be 0
    output_schema: SmokeResult  # name of the Pydantic class the router validates against
    max_output_tokens: 1024     # optional, default 2048
    ---
    System prompt text...

The body is the system prompt and is sent as is (no templating, so caller data can
never change the instructions). The caller's inputs go in the user message as JSON.
``prompt_hash`` is the SHA-256 of the whole file, so editing a published version is
detectable; publish a new version instead. Every model call records task, version and
hash.

Workflow plugins pass their own roots; the core root is ``pairly_core/llm/prompts``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml

from pairly_core.llm.catalog import TIER_ORDER
from pairly_core.llm.hashing import sha256_hex

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
DEFAULT_MAX_OUTPUT_TOKENS = 2048


class PromptError(RuntimeError):
    """A prompt file is missing or malformed (a deployment error, never user input)."""


@dataclass(frozen=True, slots=True)
class Prompt:
    task: str
    version: str
    tier: str
    temperature: float
    output_schema: str
    max_output_tokens: int
    system: str
    hash: str


def parse_prompt(raw: bytes, *, task: str, version: str) -> Prompt:
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise PromptError(f"{task}/{version}: missing front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise PromptError(f"{task}/{version}: unterminated front matter")
    try:
        meta: Any = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise PromptError(f"{task}/{version}: invalid front matter: {exc}") from None
    if not isinstance(meta, dict):
        raise PromptError(f"{task}/{version}: front matter must be a mapping")
    body = text[end + 5 :].strip()
    if not body:
        raise PromptError(f"{task}/{version}: empty prompt body")
    if meta.get("task") != task or str(meta.get("version")) != version:
        raise PromptError(f"{task}/{version}: front matter task/version do not match the path")
    tier = meta.get("tier")
    if tier not in TIER_ORDER:
        raise PromptError(f"{task}/{version}: tier must be one of {TIER_ORDER}")
    temperature = meta.get("temperature")
    if temperature != 0 or isinstance(temperature, bool):
        raise PromptError(f"{task}/{version}: temperature must be 0")
    schema = meta.get("output_schema")
    if not isinstance(schema, str) or not _SCHEMA_RE.fullmatch(schema):
        raise PromptError(f"{task}/{version}: output_schema must name a Pydantic class")
    max_tokens = meta.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not (16 <= max_tokens <= 64_000)
    ):
        raise PromptError(f"{task}/{version}: max_output_tokens must be 16..64000")
    unknown = set(meta) - {
        "task",
        "version",
        "tier",
        "temperature",
        "output_schema",
        "max_output_tokens",
        "description",
    }
    if unknown:
        raise PromptError(f"{task}/{version}: unknown front matter keys {sorted(unknown)}")
    return Prompt(
        task=task,
        version=version,
        tier=str(tier),
        temperature=0.0,
        output_schema=schema,
        max_output_tokens=max_tokens,
        system=body,
        hash=sha256_hex(raw),
    )


Root = Path | Traversable


def core_prompt_root() -> Traversable:
    return resources.files("pairly_core.llm").joinpath("prompts")


class PromptRegistry:
    """Loads prompts from one or more roots. First root that has the file wins."""

    def __init__(self, roots: Sequence[Root] | None = None) -> None:
        self._roots: tuple[Root, ...] = tuple(roots) if roots else (core_prompt_root(),)
        self._cache: dict[tuple[str, str], Prompt] = {}

    def get(self, task: str, version: str) -> Prompt:
        if not _NAME_RE.fullmatch(task) or not _VERSION_RE.fullmatch(version):
            raise PromptError("invalid task name or prompt version")
        key = (task, version)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        for root in self._roots:
            candidate = root.joinpath(task, f"{version}.md")
            if candidate.is_file():
                prompt = parse_prompt(candidate.read_bytes(), task=task, version=version)
                self._cache[key] = prompt
                return prompt
        raise PromptError(f"prompt {task}/{version} not found")

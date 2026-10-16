# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The synthetic attack corpus, parsed once per test session."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from pairly_core.documents.model import NormalizedDocument
from pairly_core.guardrails import GuardrailReport
from synth.attacks.corpus import Sample, build_corpus
from synth.attacks.evaluate import parse_sample, scan_sample


@dataclass(frozen=True)
class Parsed:
    sample: Sample
    doc: NormalizedDocument
    report: GuardrailReport


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Parsed]:
    workdir: Path = tmp_path_factory.mktemp("attacks")
    parsed: dict[str, Parsed] = {}
    for sample in build_corpus():
        doc = parse_sample(sample, workdir)
        parsed[sample.name] = Parsed(sample, doc, scan_sample(sample, doc))
    return parsed


def names(attack: bool) -> Iterator[str]:
    return (s.name for s in build_corpus() if s.attack is attack)

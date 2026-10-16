# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Synthetic attack and clean documents for the guardrails (WP1.4).

* ``corpus``    the samples (PDF, DOCX, text), generated in memory.
* ``evaluate``  parse, scan and sanitise every sample; precision and recall.

Run ``uv run python -m synth.attacks`` for a JSON report.
"""

from synth.attacks.corpus import Sample, attack_samples, build_corpus, clean_samples

__all__ = ["Sample", "attack_samples", "build_corpus", "clean_samples"]

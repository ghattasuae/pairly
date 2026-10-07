# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Schema-driven extraction with per-field confidence and source spans (WP1.3).

* ``types``        ``Extracted[T]``, ``ExtractionModel``, ``Rule``, ``Money``, shape types.
* ``schema``       compiles a schema into field specs and the prompt's schema description.
* ``validators``   deterministic parsers and checks per field type.
* ``evidence``     ``SanitizedInput`` contract and deterministic quote location.
* ``guarded``      ``GuardedInput``: guardrails output bound to one document, the only
                   input ``extract`` accepts.
* ``engine``       ``extract``: model call, checks, thresholds, human routing.
* ``models``       ``extractions``, ``extraction_fields``, ``extraction_corrections``.
* ``service``      storage, corrections (append only) and few-shot hints.
* ``permissions``  ``core.extraction.*`` and the per-workflow permission rule.

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Live evals: the model-dependent metrics, measured with a real model.

    PAIRLY_MODEL_MODE=live uv run python -m evals.live <suite> [--max-cost-usd 5]

Suites: ``hire-extraction``, ``hire-fairness``, ``hire-injection`` (``evals.live.hire``),
``pay-extraction`` (``evals.live.pay``); ``report`` writes the "Live baseline" section of
``evals/report.md`` from ``evals/results/*_live.json``. See ``evals/live/harness.py``
for the router, the recordings and the cost cap, and ``evals/README.md``.
"""

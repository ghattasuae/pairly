# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hire screening evals (WP2.10): ``uv run python -m evals.hire``.

Writes ``evals/results/hire.json`` and the Hire section of ``evals/report.md``. The
gate (``python -m evals.gate``) compares the results with ``evals/thresholds.yaml``.

Measured for real (deterministic code, on the synthetic dataset):

* CV field extraction ceiling per field: with the ground truth as the model's answer,
  the share of values the extraction framework accepts (parsing, evidence location,
  value checks);
* evidence validity: every quote of every scoring result is found verbatim at its
  location in the original CV, outside hidden and blind-redacted text (target 100%);
* fairness: every protected marker removed from the scoring input by blind redaction,
  the scoring input identical across each counterfactual pair, the overall score
  identical for identical criterion scores;
* injection: the guardrail numbers of ``evals.guardrails_hire`` plus the end to end
  check that no attack payload reaches the scoring model input;
* claim checks on the synthetic claim attacks, knockout routing, deterministic stage
  timings, and that the committed demo recordings still answer every request.

Pending a live run (``pending_live_run``, never invented): model extraction accuracy,
rubric scoring agreement with the synthetic labels (the recordings are scripted from
those labels, so agreement on them means nothing), model score changes across the
fairness pairs, the classifier's share of injection detection, model latency and cost.
"""

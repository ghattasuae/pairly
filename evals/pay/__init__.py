# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay validation and fraud check evals (WP4.6): ``uv run python -m evals.pay``.

Writes ``evals/results/pay.json`` and the Pay section of ``evals/report.md``. The gate
(``python -m evals.gate``) compares the results with ``evals/thresholds.yaml``.

What runs: every invoice of the synthetic Pay dataset (``synth.pay``: regular invoices
and attack invoices), in arrival order, through exactly the checks the ``pay_invoice``
graph runs (``pairly_pay.checks.run``: arithmetic and VAT, duplicates, bank mismatch,
impersonation, amounts, fake-invoice signals, injection), the bundled policy rule set
and the risk combiner under the bundled risk policy. Documents are rendered and parsed
in process with the sandbox's parsers and scanned by the guardrails; scans need OCR and
are skipped for parsing unless ``--include-scans`` (their checks still run on the read
values, without a document).

**The read values are the ground truth** (what a perfect extractor reads), so the
numbers measure the checks, not extraction: a check that misses here misses with a
perfect reader too. Earlier invoices of the dataset are the history; the vendor master
is the synthetic one. No model, no spend policy document (the policy check is the
deterministic rule set, the spend policy search needs a live model).

Measured: duplicate detection recall and precision (and how often the suspected
original is the right one), bank-mismatch recall (target 1.0), lookalike-domain recall,
arithmetic and VAT error recall, injection catch rate, false positives on clean
(regular) invoices, recall per expected finding, and check errors.

Purchase order matching (brief 4.5.2) runs too, with the synthetic POs and goods
receipts on file (``evals.pay.matching``), and is measured on the synthetic matching
cases: exception recall and precision, the match rate of cases that should match, per
scenario. The fraud false-positive numbers leave matching out (a price variance on a
regular invoice is an expected exception, not a false positive).
"""

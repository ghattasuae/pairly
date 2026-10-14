# Evals

Evaluation suites that gate merges (BRIEF rule 6). Everything runs on synthetic data
from `synth/`, offline, with no model API key.

```
make evals                         # the runners below, then the gate
uv run python -m evals.guardrails_hire
uv run python -m evals.hire
uv run python -m evals.pay
uv run python -m evals.gate        # exit 1 when a measured metric misses its threshold
```

- `thresholds.yaml`: one entry per gated metric (result file, path, `min`, `max` or
  `equals`, and why). Initial values; ask before lowering any of them.
- `gate.py`: reads the thresholds and the result files. `PASS`, `FAIL` (outside the
  threshold, missing, not a number, or measured with no threshold yet), `PENDING`
  (`{"status": "pending_live_run"}`: needs a real model; listed, not gating) or `INFO`
  (`informational: true` with a `why`: listed for context, never gating, still fails
  when missing).
- `results/`: the latest result of each runner (JSON).
- `report.md`: generated sections, one per runner, and notes on the gate after them.
  Numbers quoted anywhere must come from here, never be written by hand.

The CI `evals` job runs `make evals` on every pull request and is part of `ci-ok`.

## `python -m evals.guardrails_hire`

The deterministic guardrails on the synthetic Hire corpus (`synth.hire`): every PDF and
DOCX attack CV and clean CV is parsed in process with the sandbox's parsers (renderer
check included) and scanned. Writes `results/guardrails_hire.json`: precision, recall
and false-positive rate at document level, recall per check and per technique, payload
leaks, detector errors, every false positive with its finding codes, and the share of
rubric label quotes found in the parsed text per format and layout.

Claim attacks (dates, titles, unsupported skills) are reported apart: they are for the
claim consistency checks, not the guardrails. The optional model classifier is not run
(it needs a live model). Scans need OCR and are skipped unless `--include-scans`.

## `python -m evals.hire`

The Hire screening pipeline (`pairly_hire.screening.pipeline`, exactly what the
`hire_screening` graph runs) on every PDF and DOCX of the synthetic dataset: guardrails,
the injection classifier, sanitising, CV parsing, blind redaction, scoring, claim checks
and knockouts. Writes `results/hire.json` and the Hire section of `report.md`.

The model steps are answered by `synth.hire.screening` from the synthetic ground truth.
**Those answers are scripted, not model output.** The run records them as a bundle and
replays the whole pipeline through it by request hash, so it also proves the pipeline is
deterministic. Measured for real: the CV field extraction ceiling per field (what
parsing, evidence location and value checks accept when the answer is right), evidence
validity (every quote of every scoring result is found at its location in the original
CV, target 100%), blind redaction of every protected marker, identical scoring input
across each counterfactual pair, deterministic aggregation, attack payloads reaching
the scoring model, claim checks, knockout routing, deterministic stage timings, and that
the committed demo recordings are current.

Reported as `pending_live_run` until a run with a model key exists, never estimated:
model extraction accuracy, rubric scoring agreement with the synthetic labels (the
scripted answers are made from those labels, so agreement on them means nothing), model
score changes across fairness pairs, the classifier's share of injection detection,
model latency and cost per CV.

Options: `--count`, `--seed`, `--limit N` (first N documents), `--include-scans`,
`--out`, `--report`.

## `python -m evals.pay`

The Pay validation and fraud checks (`pairly_pay.checks`, exactly what the `pay_invoice`
graph runs) on every invoice of `synth.pay` in arrival order: regular invoices and the
attack invoices. Documents are rendered, parsed in process and scanned by the
guardrails (scans need OCR and are not parsed unless `--include-scans`). The read values
are the synthetic ground truth, so the numbers measure the checks with a perfect reader,
not extraction; the spend policy search needs a live model and is not run (the
deterministic rule set is). Writes `results/pay.json` and the Pay section of
`report.md`: duplicate recall and precision, bank-mismatch recall (gate 1.0),
lookalike-domain recall, arithmetic and VAT error recall, injection catch rate, false
positives on clean invoices, recall per expected finding and check errors.

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

Reported here as `pending_live_run`, never estimated: model extraction accuracy, rubric
scoring agreement with the synthetic labels (the scripted answers are made from those
labels, so agreement on them means nothing), model score changes across fairness pairs,
the classifier's share of injection detection, model latency and cost per CV. They are
measured with a real model by `python -m evals.live` (below), in separate result files
that do not gate until the founder sets their thresholds.

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

## `python -m evals.live`

The model-dependent metrics, measured with a real model. Needs the Anthropic key (read
by the settings loader from the environment or `.env`) and `PAIRLY_MODEL_MODE=live` in
the environment of the run only (`.env` keeps `recorded`):

```
PAIRLY_MODEL_MODE=live uv run python -m evals.live hire-extraction --max-cost-usd 8
PAIRLY_MODEL_MODE=live uv run python -m evals.live hire-fairness --max-cost-usd 5
PAIRLY_MODEL_MODE=live uv run python -m evals.live hire-injection --max-cost-usd 1
PAIRLY_MODEL_MODE=live uv run python -m evals.live pay-extraction --max-cost-usd 5
uv run python -m evals.live report   # the "Live baseline" section of report.md
```

- The production `ModelRouter` with the real Anthropic provider: each prompt starts at
  its tier (small) and escalates on invalid output or low confidence, as in production.
- `hire-extraction`: the regular PDF and DOCX CVs (fixed shuffled order) through the
  screening pipeline: model CV field extraction accuracy, rubric agreement with the
  synthetic labels, evidence validity. `hire-fairness`: the 60 counterfactual pairs, the
  model's score change per pair (each CV screened on its own, with the production
  router's response reuse), reported apart for pairs with byte-identical scoring input
  (delta 0 expected) and input-different pairs (each CV screened 3 times with reuse off,
  the delta next to the model's own run-to-run spread). `hire-injection`: the
  classifier pass on the 5 paraphrased attacks and 20 clean CVs. `pay-extraction`: the
  31-invoice eval subset of `synth/pay/recordings.py`, field, line, total and VAT accuracy.
- `--max-cost-usd` (default 5) is a hard cap: the router's budget refuses a request
  whose worst case could pass it, the run stops after that document (dropped from the
  measurement) and the result is marked `partial`. `--every N` prints the spend every N
  documents; `--limit N` runs the first N documents.
- Every live response is appended at once to `evals/recordings/<product>_live.jsonl`
  with its tokens, cost, latency and the model id the API answered with; at the end of
  the run that file is folded into the committed, xz-compressed
  `evals/recordings/<product>_live.jsonl.xz` (`python -m evals.live compress` does it by
  hand; both forms are read). A later run, on any machine, replays a recording of the
  same request for the same document instead of calling the model, so it is free
  (replayed calls report their recorded cost and latency).
- `python -m evals.live compress` also prunes: it replays every suite offline (no model
  is called) to learn the requests they ask for now and drops the recordings no current
  request asks for (a reworded prompt, rubric or schema leaves the earlier answers
  behind), printing how many it kept and dropped. If a suite cannot be replayed nothing
  is dropped. What the dropped entries cost stays counted in
  `evals/recordings/<product>_live.pruned.json`, so the total live spend in the report
  does not shrink.
- Response reuse (`PAIRLY_LLM_REUSE_RESPONSES`, on by default, as in production): a
  request already answered for another document reuses that answer at no cost
  (`reused_from`). Set it to `false` in the run's environment to measure without it.
- Results: `results/hire_live.json` and `results/pay_live.json`, one key per suite,
  with the sample size, status (`complete` or `partial`), models, tokens, cost and
  latency p50/p95. A metric not fully measured is partial with its sample size, never
  estimated.

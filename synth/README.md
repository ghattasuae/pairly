# Synth

Deterministic synthetic data generators: CVs, jobs, invoices, vendors, purchase orders and attack examples. Every generator takes a fixed seed so the output is reproducible.

Only synthetic data lives in this repository. Never add real CVs, invoices, names or bank details.

* `synth/attacks`: attack and clean documents for the guardrails (WP1.4).
* `synth/expense`: three receipts for the expense example workflow (clean, over 500 AED, hidden text) and their recorded model responses. Run `uv run python -m synth.expense` to write them, `--check` to compare.

CVs, jobs, invoices, vendors and purchase orders are not implemented yet (WP2.1 and WP4.1).

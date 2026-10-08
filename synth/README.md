# Synth

Deterministic synthetic data generators: CVs, jobs, invoices, vendors, purchase orders and attack examples. Every generator takes a fixed seed so the output is reproducible.

Only synthetic data lives in this repository. Never add real CVs, invoices, names or bank details.

* `synth/attacks`: attack and clean documents for the guardrails (WP1.4).
* `synth/hire`: 3 sample jobs with rubrics, 300+ CVs (PDF, DOCX, scans; English, Arabic, bilingual), attack CVs and fairness pairs, with ground truth and synthetic rule-based labels (WP2.1). Run `uv run python -m synth.hire generate --out <dir>`; `--check` compares the committed manifests. See `synth/hire/README.md`.
* `synth/expense`: three receipts for the expense example workflow (clean, over 500 AED, hidden text) and their recorded model responses. Run `uv run python -m synth.expense` to write them, `--check` to compare.

Invoices, vendors and purchase orders are not implemented yet (WP4.1).

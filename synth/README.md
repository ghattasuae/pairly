# Synth

Deterministic synthetic data generators: CVs, jobs, invoices, vendors, purchase orders and attack examples. Every generator takes a fixed seed so the output is reproducible.

Only synthetic data lives in this repository. Never add real CVs, invoices, names or bank details.

* `synth/attacks`: attack and clean documents for the guardrails (WP1.4).
* `synth/hire`: 3 sample jobs with rubrics, 300+ CVs (PDF, DOCX, scans; English, Arabic, bilingual), attack CVs and fairness pairs, with ground truth and synthetic rule-based labels (WP2.1). Run `uv run python -m synth.hire generate --out <dir>`; `--check` compares the committed manifests. See `synth/hire/README.md`.
* `synth/expense`: three receipts for the expense example workflow (clean, over 500 AED, hidden text) and their recorded model responses. Run `uv run python -m synth.expense` to write them, `--check` to compare.
* `synth/pay`: 34 vendors with invoice templates, 320 invoices (PDF, scans, DOCX, UBL / PINT AE XML, email bodies; English, Arabic, bilingual) with ground truth per field and line, purchase orders, goods receipts and matching cases, 69 attack invoices with expected findings (the Pay demo trio included) and inbound emails (WP4.1). Run `uv run python -m synth.pay generate --out <dir>`; `--check` compares the committed manifests. See `synth/pay/README.md`.

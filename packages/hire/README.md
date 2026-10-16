# pairly-hire

Pairly Hire workflow plugin (`pairly_hire`): jobs, rubrics, the careers page, the apply flow and screening (M2, WP2.2, WP2.3 and WP2.4). The pipeline, scheduling and the candidate portal come in later work packages.

## What is here

| Area | Modules | Notes |
|---|---|---|
| Permissions | `permissions.py` | `hire.*` with default grants. Locking a rubric and publishing a job need a sign-in with a second factor. Self-identification totals are for admins and auditors only. |
| Jobs | `models.py`, `jobs.py`, `api/jobs.py` | Title, team, location, work model, job type, salary range shown or hidden, English and Arabic text, status (draft, open, paused, closed). Every write is audited. |
| AI drafts | `drafting.py`, `prompts/`, `recordings.py`, `demo.py` | Job description from a short brief, rubric from the job text. Versioned prompts, validated output, every call logged. Recorded answers for the demo inputs, so it works offline. |
| Text checks | `language/inclusive.py`, `language/sanity.py` | Inclusive-language checker (English and Arabic wordlists and patterns, with suggestions) and requirements sanity check (years of a technology older than it, contradictory seniority). Deterministic. |
| Rubrics | `rubric.py`, `rubrics.py` | The canonical rubric model. AI drafts, a person edits and locks; a lock is an immutable policy version (`hire_rubric`). Applications keep the version they arrived under; nothing is rescored silently. |
| Forms | `forms.py` | Contact fields, custom questions (text, choice, yes or no), knockout questions from the locked rubric (a different answer goes to a person), the CV and optional files, voluntary self-identification. Versioned like rubrics. |
| Careers page | `careers.py`, `api/public.py` | Public data for open jobs, `JobPosting` JSON-LD, JSON and XML feeds. Rate limited, cached. |
| Apply flow | `applying.py`, `antispam.py`, `dedupe.py`, `notice.py`, `templates.py` | AI-use notice and consent, Turnstile behind an interface (fake in development and test), per-IP and per-email limits, disposable email list, CV through the engine's ingestion, duplicate hints, acknowledgement email in English or Arabic. |
| Screening | `screening/`, `scoring/`, `api/screening.py`, `prompts/hire_cv_score` | The `hire_screening` graph per application: CV scan and parse, guardrails with the renderer check and the injection classifier (default always), sanitising, CV profile with evidence, blind redaction (name, contact, gender, age, nationality, marital status, address, universities; on by default), scoring of each criterion 0 to 4 with quoted evidence (no evidence, no credit; the overall score is computed in code from the weights), knockouts to a person with a suggested action, claim checks and AI-generated signals as information. A person always decides (`hire.screening` approval); shortlisting lifts blind mode. Decision records with replay. Reviewer panel, overrides with a reason, re-runs and settings. |
| Self-identification | `selfid/` | Its own encrypted table and permission. The screening and scoring code (`screening/`, `scoring/`) cannot import it (import-linter contract and a test). |
| Retention | `retention.py` | Rejected applications are due after the organisation's months (default 12), dry run only. |

## Commands

```sh
uv run python -m pairly_hire.recordings          # rebuild data/recordings.json after a prompt or schema change
uv run python -m pairly_hire.recordings --check  # fail when it is out of date
uv run python -m synth.hire manifests            # rebuild the scripted screening recordings too
uv run python -m evals.hire                      # screening evals (evals/results/hire.json)
make bootstrap-org NAME="Acme Logistics" SLUG=acme ADMIN_EMAIL=you@acme.test
make demo-hire SLUG=acme                         # jobs, rubrics, people, recordings
```

The careers page of the seeded organisation is at `/en/careers/acme` (and `/ar/careers/acme`).

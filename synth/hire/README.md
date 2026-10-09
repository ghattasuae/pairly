# Synthetic Hire data

Synthetic jobs, CVs, attack CVs and fairness pairs for Pairly Hire (WP2.1). Everything
is generated from a seed: no real CV, person, employer or university is used. Names come
from Faker locales, phone numbers from Faker `ar_AE`, emails and profile links use
reserved example domains (RFC 2606), so no address or profile can belong to a real
person.

```
uv run python -m synth.hire generate --out <dir> [--count N] [--seed S]
uv run python -m synth.hire --check
uv run python -m synth.hire manifests
```

* `generate` renders every document into `<dir>` (`cvs/`, `attacks/`, `fairness/`) and
  writes `<dir>/index.json`: every document with its file, ground truth and labels.
  The defaults (300 regular CVs, seed 2026) take about 10 seconds on a laptop.
* `--check` rebuilds the manifests in memory and fails when a committed one differs.
* `manifests` rewrites the committed manifests after a change to a generator.

Pass `--count` and `--seed` after the subcommand. Binary files are never committed:
only the generators and the ground truth below.

## What is committed

| File | Content |
| --- | --- |
| `jobs/*.yaml` | 3 sample jobs (backend engineer, AP accountant, recruiter) each with a Pairly Hire rubric (`pairly_hire.rubric`: must-haves, nice-to-haves, weights 1 to 10, evidence descriptions, yes or no knockout questions) and a `label_guide` (score anchors 0 to 4 and Arabic criterion names for the synthetic labeler) |
| `manifests/cvs.jsonl` | one record per regular CV: format, layout, language, ground truth, persona, labels |
| `manifests/attacks.jsonl` | attack CVs: the same, plus what each attack contains and what should flag it |
| `manifests/fairness.jsonl` | fairness CVs (bases and variants) |
| `manifests/fairness_pairs.json` | the pairs: base, variant, the one attribute that differs |
| `manifests/summary.json` | counts, Faker version, the labeling rule of every criterion |
| `recordings/guardrails_document_classify.json` | scripted classifier responses for offline tests |

The rubric model is the plugin's own (`pairly_hire.rubric.Rubric`): loading a job
validates its rubric with it, so the sample rubrics are exactly what a recruiter could
lock and cannot drift from the product. `synth` imports `pairly_hire`, never the other
way round. A knockout never rejects: an answer that differs from the rubric's
`expected_answer` routes the CV to a person (`knockout_route: human_review`).

## Documents

* **Regular CVs** (`cv-0001` ...): 8 role families (the three job families make up 60%),
  four seniority levels, careers with dates relative to a fixed date (September 2026).
  Formats: PDF (reportlab), DOCX (python-docx), PNG and JPEG scans (Pillow, with skew,
  speckle noise, blur and JPEG artefacts; a CV that does not fit one page becomes an
  image PDF). Layouts: single column, two column, sidebar (dark band with white text),
  table, Arabic (right to left) and bilingual. Languages: English 76%, Arabic 12%,
  bilingual 12%.
* **Attack CVs** (`atk-...`): hidden white and near-white text, 1 point text, rendering
  mode 3, text under a drawn shape or an opaque image, clipped text, off-page text,
  instruction injections in English and Arabic (including paraphrased ones without
  trigger words), keyword stuffing (lists, repetition, hidden), metadata injections
  (PDF subject, keywords, XMP, DOCX comments and custom properties, file name), bidi
  override, zero-width and tag characters, homoglyphs, and claim attacks: a job that
  ends before it starts, overlapping jobs, a future start date, title inflation,
  skills no role uses, an unexplained gap. Each record says the techniques, the
  guardrail finding types a detector should raise (`expected_findings`), which check
  should catch it (`caught_by`: `deterministic`, `renderer`, `classifier` or
  `claim_consistency`), the payloads that must never reach a model and, for claim
  attacks, the flags a claim check should report.
* **Fairness pairs** (`fair-NN-...`): 12 base CVs with a "Personal details" section,
  each with 5 variants that change exactly one thing: the name (another Faker locale,
  same gender, with the email and links that follow from it), the gender (first name
  and gender line), the nationality line, the birth year (20 years earlier), or a
  photo placeholder (a grey silhouette, never a face). Labels are identical across
  every pair, checked when the dataset is built.

`text_layer` says how text sits in the file: `logical` (as written), `visual_shaped`
(PDF with Arabic lines drawn as shaped presentation forms in visual order, as most PDF
producers do; extraction needs normalising before quotes match) or `image` (OCR).

## Ground truth

`ground_truth` is what a perfect CV parser extracts, in the CV's own language: contact
(name, the other-script name for bilingual CVs, email, phone, city, country, links),
summary, experience (employer, title, start and end as displayed: `YYYY-MM`, or `YYYY`
when the CV shows years only; `null` end means present), education, skills,
certifications and languages. `persona` holds the protected attributes (gender,
nationality, birth year, photo, name locale). No label reads it.

## Labels: synthetic, not human judgments

The rubric labels are produced by a transparent, deterministic rule per criterion
(`labels.py`, listed with parameters in `manifests/summary.json`). They stand in for
human-style labels so scoring evals have a reproducible reference. **They are
synthetic labels, not human judgments**: they must never be presented as reviewer
agreement or used to claim accuracy against people. Real calibration needs real
reviewers (brief section 3.3, item 11).

Each label is a score 0 to 4 plus the CV text that justifies it (`evidence`, exact
lines of the CV; empty for 0). The rules:

* `years`: months in the job's role family (related families at half weight) / 12.
  0 if none, 1 under 1 year, 2 under 3, 3 under 5, 4 from 5. Evidence: the title and
  date lines of the counted jobs.
* `demonstrated`: achievement lines whose template demonstrates the criterion's
  capabilities or uses its skills. 1, 2, 3 or more lines give 2, 3 or 4. With none, a
  matching skill named only in the skills list gives 1 (evidence: the skills line),
  unless the criterion gives no list credit (leadership, technical hiring, volume).
* `credential`: a listed certificate gives 4; else the best degree: 3 in the main
  field (4 for a master's where the rubric says so), 2 in a related field, 1 for a
  diploma; else 0.
* `language`: Native or Fluent 4, Professional 3, Conversational 2, Basic 1, none 0.

The overall score is the weighted mean as a percentage of the scale maximum
(`labels.overall_score`), computed in code. Knockout answers are synthetic application
answers, "yes" or "no" as the apply form takes them.

Attack CVs are labelled from what they visibly claim: hidden text adds nothing,
inflated claims are read literally. That is what a naive scorer would give, which the
guardrails and claim checks must prevent.

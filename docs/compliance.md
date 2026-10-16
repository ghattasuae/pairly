# Compliance feature mapping

**This page is a feature mapping. It is not legal advice and it is not a claim that Pairly, or any organisation using it, complies with any law.** It lists what the software does that an organisation may use when it works out its own obligations with its own lawyers. Laws change, and their application depends on facts about each employer, each role and each country. Proofly Technology makes no compliance claim.

Where a feature lives in code, the module is named, so a reviewer can check it. Security controls are in [security.md](security.md), the AI components in [model-cards/](model-cards/README.md).

## Features this page refers to

| Feature | What it does | Where |
|---|---|---|
| AI-use notice | A plain notice before applying: what the AI does, that people decide, how to ask for human review or deletion. Versioned; each application stores the version accepted, the time and the language | `pairly_hire.notice`, `pairly_hire.applying` |
| People decide | No agent rejects a candidate, sends an offer, approves a payment, changes bank details or deletes data. Graphs stop at an interrupt; the executor re-checks the human approval in the database before any final action | `pairly_core.workflow.runtime`, `pairly_core.approvals.executor` |
| Decision records | Every AI-assisted step and every human action, with inputs, policy and prompt versions, model, output, evidence, confidence, and who decided what | `pairly_core.decisions` |
| Receipts | A role-redacted page and PDF that explains one decision, in English and Arabic | `pairly_core.decisions.receipts` |
| Appeals and human review | Contest one decision by its id; a queue with an SLA, a required written outcome, segregation of duties, reported in analytics | `pairly_core.decisions.appeals`; the candidate portal's privacy centre (`pairly_hire.portal`) |
| Replay | Re-run a past decision with the same inputs, versions and settings, and see whether it matches | `pairly_core.decisions.replay` |
| Audit log | Hash-chained, append-only, with signed heads outside the database; exports; a read-only auditor role | `pairly_core.audit`, `pairly_core.audit_anchor` |
| Bias report and audit pack | Selection rates and impact ratios per self-identified group and pipeline step, small groups suppressed, exact tests for small samples; an export with the counts, results and method | `pairly_hire.analytics.bias` (`audit_pack`) |
| Self-identification kept apart | Voluntary answers stored encrypted and apart; scoring code can never import them (import-linter contracts); the AI never sees them | `pairly_hire.selfid`, `pyproject.toml` |
| Blind screening | Names, contact details and protected markers are removed from what the scoring model reads | `pairly_hire.screening.blind` |
| Fairness tests | Counterfactual copies of CVs that differ only in name, gender markers or nationality, screened and compared | `pairly_hire.analytics.fairness`, `evals/hire` |
| Calibration | People label a sample without seeing the AI's scores; agreement per criterion | `pairly_hire.analytics.calibration` |
| Compliance pack | One export of decision records, policy versions, overrides, appeals and reports for a period, with a SHA-256 manifest | `pairly_core.decisions.compliance` |
| Model cards, risk register, oversight | One card per AI component, with eval numbers generated from result files | [model-cards/](model-cards/README.md) |
| Data requests | Candidates ask for an export, deletion or human review from the portal; admins see them with their deadline | `pairly_hire.portal`, `pairly_hire.api.candidate_requests` |
| Erasure | A person confirms a deletion request; personal data goes, the decision trail stays | `pairly_hire.erasure` |
| Retention rules | Rules per table; rejected applications after the organisation's retention period. Runs as a dry run today: it counts, never deletes | `pairly_core.retention`, `pairly_hire.retention` |
| Encryption and blind indexes | Personal fields encrypted per organisation, table, column and row; search by keyed hash | `pairly_core.crypto` |
| PII kept out of logs and traces | Redaction in every log line and trace, with a count per organisation per day on the AI governance board | `pairly_core.guardrails.pii`, `pairly_core.guardrails.redaction_counts` |

## New York City Local Law 144 (automated employment decision tools)

| Topic | What Pairly provides | What stays with the employer |
|---|---|---|
| Bias audit | The bias report and its audit pack: selection rates and impact ratios per group and step, with the method written out. It is built to support an independent auditor's work. | Engaging an independent auditor, deciding whether the law applies to the way the tool is used, publishing a summary of results |
| Candidate notice | The versioned AI-use notice before applying, in English and Arabic; the accepted version is stored per application | The notice periods, content and channels the law requires, and the alternative process or accommodation it may require |
| Data collected | Self-identification is voluntary, stored apart and used only as counts | Any category the auditor needs that the form does not ask |

## EU AI Act (high-risk duties for employment uses)

AI systems used for recruitment and selection are listed as high risk. The obligations for these systems are currently scheduled to apply from December 2027. Pairly maps to the duties of the deployer and supports some of the provider's documentation duties:

| Duty | What Pairly provides |
|---|---|
| Human oversight | People decide every outcome; the AI proposes. Approvals with segregation of duties and a second factor; human override recorded as its own decision; blind mode and calibration help people judge the AI. See the [human oversight description](model-cards/human-oversight.md). |
| Logging | Every model call (`model_calls`: prompt version and hash, model, inputs, output, tokens, cost, latency); every decision record; the hash-chained audit log. |
| Technical documentation | This documentation set: architecture, ADRs, security, model cards, evals with their honesty rules, the risk register. |
| Transparency to people affected | The AI-use notice; receipts that explain a decision; the candidate portal's timeline. |
| Accuracy, robustness, cybersecurity | Eval suites that gate merges; guardrails against prompt injection; the threat model. Metrics that need a live model are marked pending until measured. |
| Risk management | The [risk register](model-cards/risk-register.md). |

Conformity assessment, registration, post-market monitoring and the quality management system are the provider's and deployer's organisational duties; the software does not perform them.

## Colorado and Illinois (notice, human review, appeal)

| Right | What Pairly provides |
|---|---|
| Notice that AI is used in an employment decision | The AI-use notice, versioned and recorded per application |
| Explanation of a decision | Decision receipts with the evidence and the versions used |
| Correction of data | Candidates can ask for human review; staff can correct extracted values, which writes a correction record |
| Human review and appeal | Appeals by decision id, with an SLA, a required written outcome and segregation of duties; requested from the candidate portal |
| Impact assessment support | The bias report, fairness runs, calibration agreement, the risk register and the model cards |

## UAE PDPL and GDPR principles

| Principle | What Pairly provides |
|---|---|
| Lawful basis and consent records | Each application stores the notice version accepted, the time and the language; talent pool consent is separate and ends on erasure |
| Data minimisation | Drafting prompts get no personal data; the scoring model reads blinded text; audit payloads hold ids, not content; MCP and webhook payloads carry ids and states |
| Purpose limitation | Self-identification answers are read only as counts for the bias report, enforced by import contracts |
| Storage limitation (retention) | Retention rules per table, configurable per organisation for rejected applications; dry run today, so deletion is not automatic yet |
| Access and portability | Candidate data export from the portal |
| Erasure | Candidate deletion requests, confirmed by a person, that remove personal data and keep only the trail |
| Security of processing | Encryption of personal fields, blind indexes, Row Level Security, the portal guard, signed links, a second factor for commitments |
| Accountability | Decision records, the audit log, the compliance pack export |
| PII in logs and traces | Redacted before writing; the number of redactions per organisation per day is on the AI governance board |
| Data flows | [security.md](security.md#data-flows-and-data-at-rest) and [architecture.md](architecture.md#a-document-from-intake-to-decision) |

## Known gaps

* Retention runs as a dry run: it reports, it does not delete.
* Vendor contacts cannot file an appeal from the vendor portal; staff file it for them.
* Metrics that need a live model (model accuracy, rubric agreement, model fairness, classifier recall) are pending in the committed eval results until a live run is committed. See [evals.md](evals.md).
* Cross-border transfer rules, records of processing, data protection impact assessments and the employer's own policies are outside the software.

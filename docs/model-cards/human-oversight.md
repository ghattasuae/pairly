# Human oversight

Pairly's rule: AI does the work, people make the call. No agent rejects a candidate, sends an offer, approves a payment, changes vendor bank details or deletes data on its own. This page describes where people oversee each AI component and how the code enforces it (brief hard rule 1, brief 6.10).

## How it is enforced

* **Interrupts.** Every workflow graph that leads to an outcome stops at a `wait` node: an approval is requested and the graph is interrupted (`pairly_core.workflow.nodes`). The [exported graphs](../graphs/README.md) show where.
* **Resume only after a decision.** `resume_run` is the only way to continue, and only after a person's decision is stored. The resume value names the approval; the node reads the decision from the database (`pairly_core.workflow.runtime`).
* **The executor.** Before any final action it re-checks, from the database, that the approval is approved and not executed, that the payload is exactly what people approved, that every approving person held the permission with a second factor and was not the submitter or a preparer, and that they still hold it (`pairly_core.approvals.executor`).
* **Least privilege for agents.** The agent role and API keys can read and propose only; they can never approve, decide, manage, export, lock, publish or verify (`pairly_core.rbac`).
* **Fail closed.** If a model, a parser, a scan or a check cannot run, the item goes to a person with the reason. Nothing is approved on a failure.

## Oversight points per component

| Component | What the person sees | What the person does |
|---|---|---|
| CV parsing and rubric scoring | Per criterion: score, confidence, evidence quotes highlighted in the CV, guardrail findings; blind mode hides names and contact details | Shortlists or not (`hire.candidate.decide`, second factor); can override scores with a reason, recorded as a human override |
| Job description and rubric drafting | The draft next to the deterministic checks | Edits; saves the job; locks the rubric (`lock`, second factor) and publishes the job (`publish`, second factor) |
| Interview kits | Questions mapped to criteria, marked as AI or template | Chooses what to ask |
| Debrief and reference summaries | The summary next to the numbers and the answers | Decides advance, hold or reject with a reason (second factor) |
| Message personalisation, exception notes | The draft that passed the checks | Edits; another person approves the exact text before it is sent |
| Document and span classifiers | Findings explained in English and Arabic, with the page and position | Reviews the flagged document |
| Invoice extraction | Values with evidence on the page, confidence, check results | Corrects values (recorded as corrections); approves the invoice (second factor) |
| Policy check | Findings with the policy excerpt they rely on | Decides the exception |
| GL coding suggestion | The suggestion with its confidence and source | Confirms or corrects the codes |
| Rubric clarification | The suggestion and the agreement report | Edits the rubric and locks a new version |

## Checking the oversight

* **Decision records** keep the AI output and the human action as separate records; a human override supersedes the AI's record (`pairly_core.decisions`).
* **Override rates** per workflow are on the AI governance board.
* **Calibration** compares people's labels, made without seeing the AI's scores, with the AI's scores per criterion (`pairly_hire.analytics.calibration`).
* **Appeals** let a candidate ask for a person to review a specific decision; the outcome needs a written reason from someone other than the original decider.
* **Replay** re-runs a past decision to show whether the AI part is reproducible.

# ADR 0008: Workflow runtime, approvals and signed links (WP1.8)

Status: accepted

## Context

Hard rule 1 of the brief: agents propose, humans decide, and this is enforced in code
(LangGraph interrupts plus server-side checks), never only in prompts. The workflow
runtime uses LangGraph with the Postgres checkpointer from `langgraph-checkpoint-postgres`.
Its tables have no organisation column and the saver runs its own SQL on a psycopg
connection, so on their own they sit outside our Row Level Security model. Approvals
also need email and Slack links that cannot be abused by mail scanners or by whoever
gets hold of the link.

## Decision

### Checkpoints stay inside one organisation

1. The checkpoint tables live in their own schema, `lg_checkpoints`, created by Alembic
   (migration 0006) from the package's DDL. The app never runs `setup()`. Startup fails
   if the installed package expects a newer checkpoint migration.
2. Each table gets `org_id uuid NOT NULL DEFAULT app_current_org_id()`, forced RLS with
   the standard tenant policy, and a check that the thread id starts with that org id.
3. `TenantCheckpointer.bind(org)` opens a dedicated connection as the application role,
   sets `app.org_id` for that whole session, hands it to the saver and closes it. It is
   never pooled, so the setting cannot leak to another tenant.
4. Thread ids are `"<org_id>:<run_id>"` (a check constraint on `workflow_runs` enforces
   it). `OrgBoundSaver` refuses any thread id of another organisation before any SQL,
   and the runtime loads the run under RLS and checks its thread id before every start
   and resume.
5. Checkpoint data is deserialised in LangGraph's strict mode (safe types only).

Session-level settings need session pooling: behind PgBouncer in transaction mode the
checkpointer must use a session-mode pool or a direct connection.

### Resume only after a human decision, and re-check before acting

* A node that needs a decision creates an `approvals` row (idempotent per run, step key
  and revision) and a following node calls `interrupt()`.
* `WorkflowRuntime.resume_run` is the only resume path. It locks the run and resumes only
  when the run waits for an approval of its own that has a recorded decision. Refusals
  are audited.
* The waiting node ignores the resume value and reads the decision from the database. A
  forged `Command(resume=...)` sent straight to the graph just interrupts again.
* Final actions run only through the executor. It locks the approval and re-derives it
  from the decision rows: right run, step key and workflow; status approved and not yet
  executed; payload hash equal to what was approved; every step complete for its mode;
  every decider a person with the permission and a second factor, assigned, delegated
  (with a delegation covering the decision time) or escalated; no submitter, preparer
  or two-step decider; every approver still holds the permission. The action runs in
  the same transaction that marks the approval executed and writes the audit event.

### Approvals

* Rules are policy-as-code YAML validated by Pydantic, stored immutably in a generic
  `policy_versions` table (reusable by WP1.5). Every approval records the version used.
  No matching chain fails closed.
* Each approval type declares its workflow permission (for example
  `pay.invoice.approve`). The inbox lists only approval types whose permission the
  actor holds; deciding calls `authorize`, which also enforces the TOTP step-up rule.
  Admins do not approve by default.
* Decisions are idempotent with an `Idempotency-Key`; decisions on one approval are
  serialised by a row lock.

### Signed links land on a confirmation page

* Token: 32 random bytes, stored as SHA-256, bound to approval, step, approval state
  version, user and action. Default lifetime 30 minutes, single use, invalidated by any
  approval state change.
* `POST /v1/approval-links/describe` only describes what the link would do. Opening
  the link or a mail scanner fetching it changes nothing.
* `POST /v1/approval-links/confirm` needs a signed-in session of the bound user and runs
  the same `decide` as the inbox, so approving needs a TOTP-backed sign-in. The link is
  never an anonymous approval. Emails point at the web page `/approvals/link#<token>`
  (the fragment never reaches a server); the page signs the user in, calls describe and
  confirms on an explicit click. The token travels only in JSON bodies, never in a URL,
  so it stays out of access logs and traces (the same rule as invitations).

## Consequences

* One extra database connection per graph invocation (opened and closed per call).
* A run whose process dies while executing stays `running`; automatic lease-based
  recovery is a follow-up. Decided runs that were never resumed are picked up by the
  worker's reconciliation job every minute.
* Changing a graph's version leaves in-flight runs of the old version waiting (resume is
  refused and audited) until a migration path for runs exists.

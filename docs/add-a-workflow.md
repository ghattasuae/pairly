# Add a workflow

This guide builds a small third workflow, expense notes, and uses it to show every extension point, end to end. The finished code is in [`packages/expense`](../packages/expense). It is also the M1 acceptance workflow, so its end to end test ([`apps/api/tests/test_expense_acceptance.py`](../apps/api/tests/test_expense_acceptance.py)) proves each step below.

The point to prove: adding a workflow needs no change in the core engine. You write a Python package, register it under one entry point, ship your migrations in it, and the engine finds the rest at runtime. The few generic extension points this needed were added together with this example and are listed in [ADR 0009](adr/0009-generic-workflow-extension-points.md).

## What the workflow does

An employee uploads a receipt with an optional note. Pairly:

1. stores the receipt (hashed, virus scanned, parsed in a sandbox),
2. scans it for prompt injection (hidden text, instructions to the model) and removes what it finds from the text a model will see,
3. reads the merchant, date, total, currency, category and description with a model, each value with a quote from the receipt as evidence,
4. checks the expense policy (receipt over 200 AED, category limits, weekends),
5. scores the risk,
6. asks a person to approve: under 500 AED one approver, from 500 AED an approver and then a finance controller. The submitter can never approve their own note,
7. after approval, marks the note reimbursable. No money moves,
8. tells the submitter.

Every step writes audit events and decision records. Every step fails closed: if it cannot run, the note still goes to a person, with the reason.

## What the engine gives you, and what you write

| The engine (`pairly_core`) | Your package |
|---|---|
| Upload intake, virus scan, sandboxed parsing | Which file types and sizes you accept (intake policy settings) |
| Guardrails scan and sanitising | Nothing: you call them |
| Extraction with evidence and confidence | The schema: fields, types, thresholds |
| Rules engine, versioned policies | The rules, as YAML |
| Risk detectors and the combiner | The risk policy, as YAML, and which detectors run |
| Workflow runtime, interrupts, approvals, SoD, executor | The graph, the approval rules, the final action |
| Decision records, audit chain, notifications, webhooks | Templates, event names |
| RBAC, tenancy, Row Level Security | Permission names and default roles, your table |

## 1. The package

Copy the layout of `packages/expense`:

```
packages/expense/
  pyproject.toml            # depends on pairly-core and pairly-api, declares the entry point
  pairly_expense/
    plugin.py               # the plugin object
    permissions.py          # expense.* permissions and default roles
    schema.py               # extraction schema
    policy/*.yaml           # policies as code, version 1
    policies.py             # publishes version 1 per organisation
    models.py               # the expense_notes table
    migrations/             # the Alembic branch "expense"
    graph.py                # the expense_note graph
    actions.py              # the final action
    api.py                  # routes under /v1/expense
    templates.py            # notification templates
    seed.py                 # demo seed
    data/recordings.json    # recorded model responses for the demo receipts
  tests/
```

The entry point is the only registration:

```toml
[project.entry-points."pairly.workflows"]
expense = "pairly_expense.plugin:plugin"
```

The entry point name must equal the plugin id, a lowercase identifier. Discovery fails closed: a plugin that does not load, has a bad id or declares a permission outside its namespace stops the API and the worker at startup.

Then add the package to the places that list workspace members (configuration, not engine code):

* `pyproject.toml`: workspace members, sources, the root dependency list, ruff `known-first-party`, the import-linter `root_packages` and contracts, and the strict mypy override if you want it (the expense package uses it).
* `infra/docker/python.Dockerfile`: the two `COPY` lines, so the image installs it.
* `Makefile` and `.github/workflows/ci.yml`: the mypy step.

### The import-linter contracts

The boundaries are checked on every `make lint` (`uv run lint-imports`). Add your package to each list:

```toml
[tool.importlinter]
root_packages = ["pairly_core", "pairly_hire", "pairly_pay", "pairly_expense", ...]

[[tool.importlinter.contracts]]
name = "Core engine never imports a workflow or an app"
type = "forbidden"
source_modules = ["pairly_core"]
forbidden_modules = ["pairly_hire", "pairly_pay", "pairly_expense", "pairly_api", ...]

[[tool.importlinter.contracts]]
name = "Workflows are independent of each other"
type = "independence"
modules = ["pairly_hire", "pairly_pay", "pairly_expense"]

[[tool.importlinter.contracts]]
name = "Apps discover workflows at runtime, never import them"
type = "forbidden"
source_modules = ["pairly_api", "pairly_worker", "pairly_mcp"]
forbidden_modules = ["pairly_hire", "pairly_pay", "pairly_expense"]
```

The first proves the engine knows nothing about your workflow; the third proves the apps need no change for it. Your package may import `pairly_core` and, for its routes, `pairly_api` (upload plumbing, idempotency, problem responses). Add contracts of your own for rules inside your package (Hire has several that keep self-identification answers away from scoring).

## 2. The plugin object

`plugin.py` declares everything on one object. The required part is the `WorkflowPlugin` protocol in `pairly_core.plugins`: `id`, `name`, `permissions`, `graphs`. Everything else is optional and read with `getattr`, so you declare only what you use:

```python
@dataclass(frozen=True, slots=True)
class ExpensePlugin:
    id: str = "expense"
    name: str = "Pairly Expense"
    permissions: tuple[PermissionSpec, ...] = field(default=PERMISSIONS)
    graphs: tuple[GraphSpec, ...] = (EXPENSE_NOTE,)
    webhook_events: tuple[EventType, ...] = WEBHOOK_EVENTS
    demo_seeds: Mapping[str, DemoSeed] = field(
        default_factory=lambda: {"expense-demo": seed_expense_demo}
    )

    def api_router(self) -> APIRouter:
        from pairly_expense.api import router  # imported only by the API

        return router
```

Other optional attributes: `migrations_dir` (section 6), `scheduled_jobs` (section 9), `prompt_roots` (section 11), `document_intake` (section 13), `dashboard_sections` (section 14), `feature_flags`, `search_sources`, `retention_rules`. Each registry checks that names stay inside your namespace. The full table is in [architecture.md](architecture.md#the-plugin-contract).

## 3. Permissions

Permissions are `<workflow>.<resource>.<action>` with the built-in roles that hold them by default (`permissions.py`):

| Permission | Who by default | Why |
|---|---|---|
| `expense.note.read` | staff, auditor, read only, agent | see notes (your own unless you review) |
| `expense.note.submit` | staff | start a run (a `submit` action may start a run) |
| `expense.note.approve` | approver, finance controller | decide; an `approve` action always needs a second factor |
| `expense.document.read`, `.upload` | reviewers; staff | the engine uses these instead of `core.document.*` for this workflow |
| `expense.extraction.read`, `.correct` | reviewers; admin and AP clerk | same rule for extractions |
| `expense.appeal.create` | staff | contest a decision of this workflow |

Rules the engine enforces, whatever you declare: the agent role only gets `read` and `propose`; API keys only `read`, `propose` and `upload`; a membership limited to one workflow grants only that workflow's permissions; `approve`, `decide`, `manage`, `export`, `lock`, `publish` and `verify` need a TOTP sign-in ([ADR 0010](adr/0010-plugin-prompts-and-commitment-step-up.md), [ADR 0012](adr/0012-verify-step-up.md)).

## 4. What to read from the document

An extraction schema is a Pydantic model of `Extracted[T]` fields (`schema.py`):

```python
class ExpenseReceipt(ExtractionModel):
    schema_name: ClassVar[str] = "expense.receipt"
    schema_version: ClassVar[str] = "1"

    merchant: Annotated[Extracted[str], Rule(required=True, threshold=0.8)]
    date: Annotated[Extracted[dt.date], Rule(required=True, threshold=0.85, date_order="DMY")]
    total: Annotated[Extracted[Money], Rule(required=True, threshold=0.9, default_currency="AED")]
    currency: Annotated[Extracted[Currency], Rule(required=True, threshold=0.8)]
    category: Annotated[Extracted[ExpenseCategory], Rule(required=True, threshold=0.6)]
    description: Annotated[Extracted[str], Rule(threshold=0.6, max_length=300)]
```

The engine asks the model for each value with a verbatim quote, checks the quote is in the text the model saw and maps it back to the original document. A value without evidence gets confidence 0 and the `no_evidence` flag. A value below its threshold gets `low_confidence`. Use only unflagged values (or a person's correction) for checks; `notes.receipt_values` shows how.

## 5. Policies as code

Three YAML documents in `policy/`, all named `expense.note`, published per organisation as version 1 (`policies.ensure_policies`). Admins publish later versions; every check records the exact version it used.

* `rules.yaml` (`pairly.rules/v1`): deterministic rules over declared facts. A fact you cannot provide is left out, and a rule that needs it gives `error`, which sends the item to a person. The expression language has no weekday function, so the graph computes `is_weekend` as a fact.
* `risk.yaml` (`pairly.risk_policy/v1`): how risk signals combine. The engine adds hard rules on top: a critical signal or a check that could not run always needs a person.
* `approval_rules.yaml` (`pairly.approval_rules/v1`): chains chosen by the approval payload. The expense rules select on `amount_aed`; when the amount is unknown no condition holds and the last, two step chain applies.

## 6. Your table and migration

Tables live in your package (`models.py`) on `pairly_core.db.Base` with `OrgScoped`. Migrations live in your package too, as their own Alembic branch:

* Declare the directory on the plugin: `migrations_dir = Path(__file__).resolve().parent / "migrations"`. The Alembic environment (`apps/api/alembic/env.py`) discovers plugins through the entry points, adds every `migrations_dir` to the version locations, and registers your tables for `alembic check`.
* The first revision has `down_revision = None`, `branch_labels = ("<plugin id>",)` and `depends_on` the core revision it needs. The expense branch starts at `expense_0001` and depends on core `0015` (the `employee` role). Later revisions chain on your own: `down_revision = "<plugin id>_0001"`.
* Name revisions `<plugin id>_NNNN` so they never clash with core's numbers or another workflow's.
* There are several heads now, so migrations always run `alembic upgrade heads` (Makefile, CI, the compose `migrate` job and the tests do). `alembic heads` and `alembic history` do not run the environment file, so they only list core's chain; `alembic current` and `upgrade` see everything.
* To write a new revision: `uv run alembic -c apps/api/alembic.ini revision --head <plugin id>@head --version-path packages/<plugin>/pairly_<plugin>/migrations -m "..."`, then write the operations by hand.

Always call `org_rls_statements`, which forces Row Level Security and grants the application role only what you list. Point at other tenant rows by `(org_id, id)` where the parent has that key, so a row can never reference another organisation's.

Then close the table to portal sessions: call `portal_guard_statements(table)` in the same migration, or add your own restrictive `<table>_portal_subject` policy if a portal of yours needs its rows. `apps/api/tests/test_portal_guard.py` fails for any org-scoped table without one. See [security notes](security.md#portal-sessions).

The expense branch, as it stands:

| Revision | What it does |
|---|---|
| `expense_0001_expense_notes` | The `expense_notes` table with RLS, and the system notification templates. Depends on core `0015` |
| `expense_0002_stuck_notes` | `expense_stuck_note_org_ids`, a SECURITY DEFINER function that returns organisation ids only, for the sweeper (section 9) |
| `expense_0003_closed_notes` | The `closed` status: a reviewer closed a note that needed review |
| `expense_0004_portal_guard` | Closes `expense_notes` to portal sessions (it was created before the guard existed) |

`make ci-test` and `make local-test` run `alembic upgrade heads` and `alembic check` on a fresh database, so a model and its migration that disagree fail the build.

## 7. The graph

A graph is a LangGraph `StateGraph` that the runtime compiles with a tenant-bound checkpointer. Declare it with a `GraphSpec`:

```python
GraphSpec(
    workflow="expense",
    name="expense_note",
    version=1,
    build=build_graph,
    start_permission="expense.note.submit",
    read_permission="expense.note.read",
    approval_types=(ApprovalType("expense.note", "expense.note.approve", APPROVAL_RULES_V1),),
    final_actions={"mark_reimbursable": mark_reimbursable},
)
```

The expense graph (`graph.py`), drawn from the code in [graphs/expense_note.md](graphs/expense_note.md):

```
ingest -> wait_parse -> guardrails -> sanitize -> extract -> rules -> risk -> propose -> wait
wait -> execute | rejected | changes -> notify
```

When you add or change a graph, run `make docs` (`uv run python -m scripts.export_graphs`): it draws every registered graph into `docs/graphs/` and into [architecture.md](architecture.md#workflow-graphs), and `scripts/tests/test_export_graphs.py` fails while the committed diagrams differ from the code.

Patterns to copy:

* **Nodes get engine services from the context.** `runtime.context` is a `WorkflowContext`: the organisation, the run, `transaction()` for a tenant-bound session, and `services` (document store, model router, scanner, parse settings). A missing service is a reason for a person, never a skipped check.
* **State carries ids, not objects.** Each node reads what it needs from the database, so a resumed run after a restart behaves the same.
* **Guardrails before any model.** `scan_document(doc)`, then `GuardedInput.build(doc, report)`; `extract` only accepts a `GuardedInput`. The expense graph stores the digest of the sanitised text and refuses to extract from anything else.
* **Fail closed in every node.** Model unavailable, no recording, parse failure, infected file, guardrail error: record the reason on the note and continue to the approval. Never to an automatic outcome.
* **Always a person.** `request_approval` (once per run and step) and `await_decision` (interrupts, then reads the decision from the database, never from the resume value). The submitter is a preparer, so segregation of duties keeps them out.
* **Give the inbox what it draws.** The approval's `context` follows the web convention (apps/web/README.md): `document_id`, `evidence[]` (`field` or `label`, `quote`, `page`, `bboxes`), `guardrail_findings[]` (`type`, `code`, `severity`, `page`, `bbox`, `evidence`) and `decision_ids[]`. The approver then sees the receipt with its evidence and guardrail highlights. `pairly_expense.notes.approval_context` builds it from stored rows and keeps it under the engine's 32 KiB limit. A context may hold finite floats (box coordinates); a payload may not.
* **Final actions through the executor.** `run_final_action` re-checks the approval, the decisions, the people and the payload hash in the database before it calls your function, in the same transaction that marks the approval executed. Your function should check its own row too (`actions.py` refuses a note that is not waiting for this approval).

## 8. Routes

`api_router` returns a FastAPI router; the API mounts it under `/v1/<plugin id>`. Use the engine's request dependencies (`CurrentActor`, `OrgSession`, `Context`) and the API's upload plumbing (`receive_upload_with_fields`, `ingest_spooled`) so uploads get the same limits, sniffing, dedupe and audit as `/v1/documents`. Opt in to `Idempotency-Key` with the `idempotent` dependency. Every write needs an audit event in the request's transaction; in development and test a write without one fails the request.

Name your response models with your workflow as a prefix (`ExpenseNoteDetail`, not `NoteDetail`): OpenAPI schema names are global, and a clash makes FastAPI rename the core schema too, which changes the generated TypeScript types. Run `pnpm --filter @pairly/shared-ts gen:api` and check that the diff only adds.

The expense routes: `POST /v1/expense/notes` (multipart receipt plus `note`, 202, starts the run after the response), `GET /v1/expense/notes`, `GET /v1/expense/notes/{id}` (values with evidence spans, guardrail findings, rule results, risk, approval, decision record ids).

## 9. Scheduled jobs: nothing stays stuck

Declare jobs in `scheduled_jobs` as `JobSpec`s named `<plugin id>_...`. A job with `run` (an async function that gets a `JobContext`: sessions, the workflow runtime, the role registry) and `every_minutes` (a divisor of 60) is scheduled by the worker, one run at a time, and recorded in `job_runs` like core jobs.

The expense plugin uses one (`sweeper.py`, `expense_stuck_note_sweep`, every 5 minutes): a note still `processing` 15 minutes after its last change (`PAIRLY_EXPENSE_STUCK_MINUTES`) had its run never start, fail or stall. With no run yet, it restarts the run once, as the submitter; otherwise the note moves to `needs_review` with the reason, so a person looks. A reviewer then acts on it with `POST /v1/expense/notes/{id}/close` or `/restart`: a written reason, `expense.note.approve` with a second factor, never on their own note, and a decision record of the person's action that supersedes the note's newest record. The engine cannot see a run that never started (there is no run row yet), which is why the workflow owns this check. To find the organisations to visit without reading across tenants, the plugin's migration adds a SECURITY DEFINER function that returns organisation ids only (`expense_stuck_note_org_ids`); the job then works in each organisation under Row Level Security.

## 10. Notifications and webhooks

System templates are rows with `org_id` NULL, seeded by your migration like core's (`templates.py`, migration `expense_0001`). Templates use the locked down Jinja subset, English and Arabic. Declare webhook events in `webhook_events` (names start with your id) and publish them with `publish_event` in the same transaction as the change.

## 11. Prompts, recordings and the offline demo

The expense workflow has no prompt of its own: `extract` runs the engine's `core_extract` prompt with the expense schema. When you need your own, declare `prompt_roots` on the plugin (directories of `<task>/<version>.md` files, every task named `<plugin id>_...`). The model router loads them next to core's ([ADR 0010](adr/0010-plugin-prompts-and-commitment-step-up.md)). Each file has front matter (task, version, starting tier, temperature 0, output schema) and a body sent as is. Never edit a published version: add `v2` and point your `TaskSpec` at it, so every call records which version it used. Pairly Hire does this for its drafts (`packages/hire/pairly_hire/prompts`) and builds their recordings with `uv run python -m pairly_hire.recordings`.

Every AI component gets a model card. Add an entry for your prompt to `CARDS` in `scripts/model_cards.py` (purpose, inputs, outputs, oversight, limits, the `TaskSpec` it uses and the eval metrics that cover it), then run `make docs`. The prompt version, tiers and eval numbers are filled in by the generator; `scripts/tests/test_model_cards.py` fails while a card is stale.

In `recorded` mode the router answers only from stored responses, keyed on the exact request (prompt, model, inputs, settings). `synth/expense` generates the synthetic receipts and, for each, the request `extract` will send and a scripted answer, and writes a recordings bundle. `pairly-admin seed expense-demo` imports it. If you change a prompt, the schema or a parser, run `uv run python -m synth.expense` again; a test fails when the files are out of date.

## 12. Demo seed

`demo_seeds` maps a name to an async function that gets an owner-role session bound to the organisation. The expense seed publishes the policies, invites a submitter, an approver and a finance controller (each limited to the workflow) and imports the recordings. It is idempotent.

```
make bootstrap-org NAME="Acme Ltd" SLUG=acme ADMIN_EMAIL=you@acme.test
make demo-expense SLUG=acme
```

The demo submitter has the built-in `employee` role: no core permission, and only the read, submit, create and upload permissions a workflow grants it.

## 13. Batch intake: `document_intake`

The batch API (`POST /v1/documents/batch`) ingests, scans and parses every document submitted for a workflow. To also turn each one into your entity and start its run, declare a `DocumentIntake` (`pairly_core.batches.intake`). Pairly Pay does this for invoices (`pairly_pay.batch_intake`):

```python
async def handle(session: AsyncSession, actor: Actor, document: Document) -> IntakeResult:
    authorize(actor, INVOICE_UPLOAD)
    invoice, created = await create_invoice(session, document=document, ...)

    async def start(runtime: WorkflowRuntime) -> None:
        await start_after_intake(runtime, org_id, invoice_id, document_id)

    return IntakeResult(entity_type="pay.invoice", entity_id=str(invoice.id),
                        created=created, after_commit=start if created else None)

DOCUMENT_INTAKE = DocumentIntake(permission=INVOICE_UPLOAD, handle=handle, policy=pay_policy)
```

* `permission` must be one of your own; the batch API checks it once per batch instead of the generic upload permission.
* `handle` runs in the request's transaction; `after_commit` runs after the commit, never inside it.
* `policy` replaces the default intake policy (types, size) for your documents.

The expense workflow does not declare one: receipts come only through its own route, which carries the note text. Without an intake, batch-submitted documents are still ingested, scanned and parsed, and nothing more starts.

## 14. Dashboard sections

The shared boards (auditor, admin, AI governance) take sections from plugins. Declare them as `DashboardSectionSpec`s in `dashboard_sections` (`pairly_core.dashboards.model`):

```python
DASHBOARD_SECTIONS = (
    DashboardSectionSpec(Board.AUDITOR, "bank_changes", BANK_CHANGE_READ, bank_changes),
    DashboardSectionSpec(Board.AI_GOVERNANCE, "guardrails_pay", INVOICE_READ, guardrails),
)
```

* Each section names a permission of your own; viewers without it do not get the section, and the rest of the board still loads.
* The builder gets a `SectionContext` (a session bound to the viewer's organisation, the actor, the time) and returns a `Section` of counts, ids and app paths. Show numbers, not personal data. A value you do not have is `None` with a note (`no_data`), never zero.
* A section that raises is shown as unavailable; it never breaks the board. Platform operations take no plugin sections.
* `apps/api/tests/test_system_dashboards.py` checks every plugin section uses its own permission.

The expense workflow declares none; Hire and Pay do (`pairly_hire.system_dashboards`, `pairly_pay.system_dashboards`).

## 15. Tests to write

* Unit: policies parse, the approval chain by amount, rule outcomes, which values count, the generated files are current (`packages/expense/tests`).
* End to end on Postgres in recorded mode (`apps/api/tests/test_expense_acceptance.py`): the clean path through approval and the final action, hidden text that never reaches the model, approval from a signed email link, a two step chain with segregation of duties, and the fail closed paths.

## Checklist

- [ ] Package with the `pairly.workflows` entry point; id equals the entry point name
- [ ] Workspace, Dockerfile, import-linter contracts, mypy and CI list the package
- [ ] Permissions in your namespace; agents read only; approvals use an `approve` action
- [ ] Extraction schema with thresholds; checks use only evidenced values
- [ ] Policies as YAML, published as version 1 per organisation
- [ ] Table with Row Level Security, and migrations as your own Alembic branch (`migrations_dir`)
- [ ] Graph: guardrails before any model, a person before any final action, every failure goes to a person with its reason
- [ ] `make docs` run: the graph diagram and a model card for each prompt of yours
- [ ] Final action re-checks its own row
- [ ] Routes audited; idempotent submissions
- [ ] Templates and webhook events in your namespace
- [ ] A job that sends anything stuck to a person
- [ ] Recordings for the demo inputs; a seed
- [ ] `document_intake` if batch-submitted documents should start your runs
- [ ] Dashboard sections behind your own permissions, numbers only
- [ ] Tests for the happy path, injection, approvals, SoD and fail closed paths

# ADR 0009: Generic extension points for workflow plugins (M1 expense acceptance)

Status: accepted

## Context

Brief 2.1: the engine knows nothing about hiring or invoices, and adding a third workflow
must need no engine change. The M1 acceptance builds that third workflow, expense notes
(`packages/expense`), end to end: upload, extraction, guardrails, rules, risk, a human
approval from the web and from a signed email link, the final action, decision records
and a verified audit chain.

Building it showed seven places where a workflow could not plug in without editing the
engine or the apps for that workflow. Each gap is generic: Pairly Hire and Pairly Pay hit
the same ones. This record lists the changes made once, so that later workflows (and the
add-a-workflow guide) need none.

## Decision

1. **Engine services for graph nodes** (`pairly_core.workflow.services`). A
   `GraphServices` value (document store, model router, virus scanner, parse settings)
   is given to `WorkflowRuntime(services=...)` and reaches every node as
   `runtime.context.services`. The API builds it from its document services and the
   router settings (recorded mode by default); the worker from its job context. Every
   field may be `None`; a node that needs a missing service must fail closed.
   Before, a graph node had no way to read a parsed document or call the model router
   except by building its own clients from the environment.

2. **`submit` may start a run** (`pairly_core.workflow.spec.START_ACTIONS`). A person
   handing in an item (`expense.note.submit`) starts a run like `create`. `submit` is
   neither an agent nor an API key action, so agents still only start proposals.

3. **Optional plugin attributes** (`pairly_core.plugins`), read with `getattr` like
   `webhook_events` and the others:
   * `api_router`: a callable returning an `APIRouter`. The API mounts it under
     `/v1/<plugin id>` (`plugin_routers`), so a workflow can never shadow a core route
     or another workflow's. A callable, so the worker never imports route modules.
   * `demo_seeds`: `<plugin id>-<name>` to an async seed function, run by the new
     `pairly-admin seed <name> --org <slug>` as the owner role in one transaction.

4. **Appeals per workflow** (`pairly_core.decisions.scope.authorize_appeal_creation`).
   Filing an appeal accepts the core-wide `core.appeal.create` or the decision's
   workflow's own `<workflow>.appeal.create`, like reading and deciding appeals already
   did. A member limited to one workflow (an employee whose note was rejected) holds no
   core permission, so before this they could never contest a decision. The record must
   still be readable by the filer (404 otherwise).

5. **System invitations** (`pairly_core.invitations.invite_as_system`). Seeds invite
   people without a signed-in admin, like `bootstrap_organisation` does for the first
   admin: `invited_by` is NULL and the audit event names the system actor. Idempotent;
   never the agent role; the workflow scope must be a known workflow.

6. **Uploads with text fields** (`pairly_api.uploads.receive_upload_with_fields`). The
   streaming multipart receiver can return small named text fields (a note next to a
   receipt) within the existing field size cap. `receive_upload` is unchanged.

7. **Plugin tables and migrations** (`apps/api/alembic/env.py`). The Alembic environment
   discovers plugins, so tables a plugin declares on `Base.metadata` are known to
   autogenerate and `alembic check`, and adds each plugin's `migrations_dir` to the
   version locations. A plugin's migrations are its own branch (`branch_labels`, first
   revision without a parent) that `depends_on` the core revision it needs (expense:
   `expense_0001`, depends on 0015). Migrations run `upgrade heads`.

Also: an import-linter contract that the API, worker and MCP apps never import a
workflow package (they discover them through the entry point), and the new package in
the workspace, Dockerfile, mypy and CI lists (configuration, not engine code).

## Consequences

* A new workflow is a package plus a migration; the guide (`docs/add-a-workflow.md`)
  uses only these extension points.
* Graph nodes now hold the router and store the process built. Tests pass their own
  `GraphServices` (in-memory store, fake scanner, recorded-mode router) through
  `create_app(graph_services=...)`.
* The API runs a graph's early nodes inline after the response (the expense run starts
  in a background task). When ClamAV is configured the API scans and parses the receipt
  itself; otherwise the graph waits a bounded time for the worker, then sends the note
  to a person (`PAIRLY_EXPENSE_PARSE_WAIT_SECONDS`, default 30).
* `pairly-admin seed` looks an organisation up by slug through
  `admin_org_id_by_slug(slug)` (core migration 0016): SECURITY DEFINER, its own
  owner-only read policy flag, EXECUTE for the owner role only.
* Core keeps its linear chain; workflows ship and version their migrations with their
  package. `alembic heads` and `history` (which do not run `env.py`) only list core's
  chain.

## Amendments

* 2026-10-08, on acceptance: workflow migrations moved to per-plugin version locations
  (item 7); the built-in `employee` role (core migration 0015) holds no core permission
  and plugins may grant it only read, submit, create and upload permissions; the
  slug lookup of `pairly-admin seed` got its own owner-only function instead of reusing
  the inbound email lookup policy.

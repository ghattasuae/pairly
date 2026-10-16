# Architecture Decision Records

Each major decision gets a short record in this folder, named `NNNN-title.md`. Every record has its context, the decision, its consequences, a status (proposed, accepted, superseded) and the date it was taken (from the git history).

## Index

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-monorepo-and-tooling.md) | One monorepo, uv for Python, pnpm for JavaScript | accepted | 2026-10-07 |
| [0002](0002-better-auth-jwks-api-rbac.md) | Better Auth in the web app, JWKS verification and RBAC in the API | accepted | 2026-10-07 |
| [0003](0003-arq-for-jobs.md) | arq on Redis (Valkey) for background jobs | accepted | 2026-10-07 |
| [0004](0004-row-level-security-tenancy.md) | Tenant isolation with Postgres Row Level Security | accepted | 2026-10-07 |
| [0005](0005-hash-chained-audit-log.md) | A hash-chained, append-only audit log per organisation | accepted | 2026-10-07 |
| [0006](0006-recorded-model-mode.md) | Recorded model mode for demos, tests and evals | accepted | 2026-10-07 |
| [0007](0007-native-local-stack.md) | A native local stack for development, Docker for CI and self-hosting | accepted | 2026-10-08 |
| [0008](0008-workflow-runtime-approvals-signed-links.md) | Workflow runtime, approvals and signed links | accepted | 2026-10-07 |
| [0009](0009-generic-workflow-extension-points.md) | Generic extension points for workflow plugins (includes per-plugin Alembic branches) | accepted | 2026-10-08 |
| [0010](0010-plugin-prompts-and-commitment-step-up.md) | Plugin prompts, and a second factor for locking and publishing | accepted | 2026-10-09 |
| [0011](0011-scheduling-in-the-engine-calendar-providers.md) | Scheduling math and calendar providers in the engine, interviews in Hire | accepted | 2026-10-09 |
| [0012](0012-verify-step-up.md) | A second factor for recording an out-of-band verification | accepted | 2026-10-14 |
| [0013](0013-portal-guard.md) | A restrictive portal guard on every core table | accepted | 2026-10-14 |
| [0014](0014-step-up-on-commitments.md) | A second factor for every approve, decide, manage, export, lock, publish and verify | accepted | 2026-10-07 |
| [0015](0015-erasure-keeps-the-trail.md) | Erasure removes personal data and keeps only the decision trail | accepted | 2026-10-10 |
| [0016](0016-generic-journal-first.md) | A generic journal export before accounting system formats | accepted | 2026-10-16 |

Records 0001 to 0007 and 0013 to 0016 were written in M6 for decisions taken earlier without a record; the date is when the decision landed in the code. Per-plugin Alembic branches have no record of their own: [ADR 0009](0009-generic-workflow-extension-points.md) (item 7 and its amendment) covers them.

## Writing a new record

Copy the shape of an existing one: `# ADR NNNN: <decision in a few words>`, `Status:`, `Date:`, then Context, Decision, Consequences. Keep it short. When a decision changes, add an Amendments section or write a new record that supersedes the old one, and update this index.

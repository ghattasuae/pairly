# ADR 0004: Tenant isolation with Postgres Row Level Security

Status: accepted

Date: 2026-10-07

## Context

Many organisations share one database. A forgotten `WHERE org_id = ...` in one query must not leak another tenant's candidates or invoices (brief 6.3).

## Decision

* Every org-scoped table has Row Level Security enabled and forced, with a policy on `app_current_org_id()`, which reads the transaction-local setting `app.org_id` (`pairly_core.migration_helpers.org_rls_statements`). With no organisation set, policies match no rows.
* The application connects as a role that owns no table and cannot bypass RLS. The API and the worker refuse to start with a role that can (`pairly_core.db.check_app_role`). Migrations run as a separate owner role.
* The session sets `app.org_id` at the start of every transaction, so the setting never leaks to the next request through the pool.
* The ORM adds `org_id = <current org>` to every query on a tenant model and refuses writes for another organisation, as a second layer.
* Work that must look across organisations (jobs, link lookups) uses narrow SECURITY DEFINER functions that return ids only, then works in each organisation under RLS.

## Consequences

* Every migration that creates an org-scoped table must call `org_rls_statements`. `apps/api/tests/test_tenant_isolation.py` proves both layers with raw SQL and ORM queries across two organisations, and `apps/api/tests/test_portal_guard.py` lists every org-scoped table of the migrated database.
* Portal sessions narrow this further with a portal subject ([ADR 0013](0013-portal-guard.md)).
* Connection poolers in transaction mode are fine for the app; the LangGraph checkpointer needs session pooling.

# Security notes

Notes on how Pairly enforces its boundaries. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Tenant isolation

Every org-scoped table has Row Level Security enabled and forced, with a policy keyed on `app.org_id` (`pairly_core.migration_helpers.org_rls_statements`). The application connects as a role that owns no table and cannot bypass RLS. The ORM adds the same organisation filter on top (`pairly_core.db`).

## Portal sessions

Candidates (Pairly Hire) and vendor contacts (Pairly Pay) sign in to portals. They are not staff. A portal request binds its database session to the organisation and to a portal subject: `app.portal_subject` is `hire_candidate:<uuid>` or `pay_vendor:<uuid>` (`pairly_core.db.set_portal_subject`). Staff, agents and jobs never set it.

Two layers of restrictive policies apply while a portal subject is set. Restrictive policies are combined with AND, so each layer can only narrow what the other allows.

1. **Plugin tables.** Each plugin keys policies on its own subject (`hire_0004b` and later, `pay_0012`). A portal session sees only its own candidate's or vendor's rows, and nothing reviewers or AP wrote.
2. **Core tables (the portal guard, migrations `0022` and `0023`).** Every org-scoped core table and `users` has four restrictive policies, one per command: `<table>_portal_guard_select`, `_insert`, `_update` and `_delete`. While a portal subject is set, the table shows no rows and accepts no writes. A code path that queries a core table from a portal request by mistake sees nothing. Plugin tables with no portal policy of their own get the same guard (`portal_guard_statements`). Examples: `expense_notes`, `hire_org_settings`, `hire_messaging_settings`.

### Narrow exceptions

A rule in `portal_guard_rules` names a table, a kind of portal person (`*` for any), the commands it allows (`commands`: SELECT, INSERT, UPDATE, DELETE) and SQL conditions for rows shown and rows written. Each condition is tied to the subject. `portal_guard_apply(table)` rebuilds the table's four policies from all of its rules, each from the rules that name its command, so each plugin adds its own rules without touching anyone else's. Only migrations write rules: the application role has no privilege on the table.

No rule allows UPDATE or DELETE today: a portal person's own uploads are insert and read only. A command no rule of the table names is closed (SELECT, UPDATE and DELETE see no row; INSERT is refused).

| Table | Who | Shown (SELECT) | Written (INSERT) |
|---|---|---|---|
| `documents` | any portal person | files they uploaded (`portal_subject`, filled by the database from the setting) | only as themselves |
| `templates` | any portal person | yes (the email texts) | never |
| `webhook_outbox` | any portal person | never | yes (an upload emits `document.received`) |
| `audit_events` | candidate | never | events with the candidate as actor |
| `audit_events` | vendor | never | events with one of the vendor's portal contacts as actor |
| `decision_records` | candidate | decisions about their own applications and CVs, and steps they took | only as themselves |
| `decision_records` | vendor | steps one of the vendor's contacts took | only as such a contact |
| `appeals` | candidate | their own (`hire_candidate:<id>`) | only their own |
| `documents` | candidate | the CVs of their applications, the files of their document requests | (see above) |
| `policy_versions` | candidate | the rubric versions of decisions they can see | never |
| `hire_jobs`, `hire_pipeline_configs` | candidate | the jobs, and their stages, of their own applications | never |

Every other org-scoped table shows a portal session nothing and refuses its writes. That includes notifications, approvals, workflow runs, model calls, search chunks, extractions, memberships, users and API keys.

### System steps of a portal request

Some engine work caused by a portal action is not the portal person's to see. `pairly_core.db.outside_portal(session)` clears the portal subject for one block of such work in the same transaction and then restores it. The organisation boundary still applies. Pending writes are flushed first, so they are checked against the portal policies. It is used only for:

* queueing a notification (`pairly_core.notifications.service.notify`): recipient preferences, addresses and dedupe rows;
* finding Hire's admins for a notice about a data request (`pairly_hire.portal.notices`);
* finding the head of a decision chain (`record_decision`): the id only.

The audit chain head (sequence and hash only) comes from `audit_chain_head()`, and the chain link trigger reads it the same way. A portal person's event therefore continues the chain, but the portal never reads the log.

A portal upload of bytes that someone else's file already has gets a plain 409 ("already received"), which names nothing.

### Tests

`apps/api/tests/test_portal_guard.py` lists every org-scoped table in the migrated database and checks:

* each table has a restrictive portal policy;
* after real vendor and candidate portal flows, a portal session of anyone else sees no row of any table except the shared templates and Pay's settings;
* the portal person sees, among core tables, only what the rules above allow;
* writes outside the rules are refused;
* every rule names its commands and none allows UPDATE or DELETE; a forged UPDATE or DELETE on every exception table, by the vendor and by the candidate, touches no row.

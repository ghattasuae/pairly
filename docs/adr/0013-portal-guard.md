# ADR 0013: A restrictive portal guard on every core table

Status: accepted

Date: 2026-10-14

## Context

Candidates (Hire) and vendor contacts (Pay) sign in to portals. A portal session is bound to the organisation, so plain RLS would show it every row of that organisation. Plugin tables already had policies keyed on the portal subject, but core tables (documents, decision records, approvals, model calls and the rest) did not. One query from a portal route to a core table by mistake would have shown staff data.

## Decision

* A portal request also sets `app.portal_subject` (`hire_candidate:<uuid>` or `pay_vendor:<uuid>`). Staff, agents and jobs never set it.
* Every org-scoped core table, and every plugin table without a portal policy of its own, gets four restrictive policies, one per command (migrations 0022 and 0023, `portal_guard_statements`). While a portal subject is set, the table shows nothing and accepts nothing.
* Exceptions are rows in `portal_guard_rules`: a table, a kind of portal person, the commands allowed and conditions tied to the subject. `portal_guard_apply(table)` rebuilds the policies from the rules. Only migrations write rules. No rule allows UPDATE or DELETE.
* Engine work caused by a portal action that the portal person may not see (queueing a notification, finding a decision chain head) runs in `outside_portal(session)`, which clears the subject for one block and restores it.

## Consequences

* A mistake in a portal route returns nothing instead of leaking.
* `apps/api/tests/test_portal_guard.py` fails for any org-scoped table without a portal policy, so a new table cannot forget it.
* The rule table is documented in [security.md](../security.md#portal-sessions).

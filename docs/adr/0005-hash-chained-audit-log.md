# ADR 0005: A hash-chained, append-only audit log per organisation

Status: accepted

Date: 2026-10-07

## Context

Brief 2.2.10: an append-only audit log where tampering is detectable, with exports and a read-only auditor role. Decision records and replays rely on it.

## Decision

* Each organisation has its own chain in `audit_events`. Each event stores the previous event's hash and its own: `sha256(prev_hash || canonical_event_json(event))` (`pairly_core.audit`).
* `append_audit` is the only writer. It takes a per-organisation advisory lock and commits with the change it records.
* The database enforces it (migration 0002): the application role has INSERT and SELECT only, triggers reject UPDATE, DELETE and TRUNCATE for every role, and an insert trigger rejects a row that does not link to the end of its chain.
* Payloads hold ids and short facts, never document contents, tokens or secrets.
* Later hardening: signed chain heads outside the database, written nightly to write-once storage, so removing the newest events is detected too (`pairly_core.audit_anchor`, WP1.10b).

## Consequences

* Verification finds the first broken `seq`. The auditor board checks the chain on each visit when it is small enough, else shows the latest stored verification.
* Erasure never edits the audit log; it holds ids, not content ([ADR 0015](0015-erasure-keeps-the-trail.md)).
* Writes are serialised per organisation. That is acceptable at the volumes of one tenant.

# ADR 0015: Erasure removes personal data and keeps only the decision trail

Status: accepted

Date: 2026-10-10

## Context

Candidates can ask for their data to be deleted (brief 6.9). The audit log is append-only and decision records must stay verifiable for audits and appeals. Hard rule 1: only people delete data.

## Decision

* Erasure is a person's action: an admin with `hire.settings.manage` (second factor) confirms the candidate's open deletion request with a written reason. Nothing starts an erasure on a schedule or from an agent.
* Everything that names or describes the candidate is cleared: contact details, CV text and files, form answers, messages, notes, evidence quotes, interview notes, compensation, references, self-identification answers, the sensitive part of decision records and the bodies of model calls about them.
* What stays is what the trail needs: ids, references, hashes, sizes, timestamps and outcomes (stages, statuses, scores, decisions). Audit events are never changed; they hold ids, not content.
* Two steps (`pairly_hire.erasure`): one transaction clears the rows and marks the request started; after commit the stored files are deleted, then the request completes. If the store fails, the request stays open with the error and a job retries.

## Consequences

* After erasure, `verify_record` answers `erased` for the candidate's decision records instead of failing.
* Bias reports and audits still count the decisions, without the person.
* A document another candidate's application also references is left alone.

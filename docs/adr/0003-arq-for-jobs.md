# ADR 0003: arq on Redis (Valkey) for background jobs

Status: accepted

Date: 2026-10-07

## Context

The engine needs background work: virus scans and parsing after upload, workflow resumes, notification delivery, webhooks, SLA escalation, nightly audit verification and anchoring, retention counts, plugin sweeps. The stack is async Python (FastAPI, SQLAlchemy async). The brief left arq or Celery open.

## Decision

* arq, an asyncio job queue on Redis. Locally and in CI the server is Valkey, which speaks the same protocol.
* Jobs are serialised as JSON, never pickle, so data in the queue cannot execute code (`pairly_worker.main`).
* Scheduled jobs are arq crons, declared in a job registry (`pairly_core.jobs.registry`; plugins add theirs with `scheduled_jobs`). A job that is not declared is refused when the worker starts. Every run is recorded in `job_runs`.
* Enqueueing is best effort. Every job that matters has a sweeper that finds stalled work by its status in Postgres and queues it again (for example `sweep_documents`), so a lost message delays work but never loses it.

## Consequences

* One async code base for the API and the worker; graph nodes run in either.
* Postgres, not the queue, is the source of truth for what is pending.
* Per-process state (such as the PII redaction tally) cannot rely on a cron, since a cron job runs in one worker only; such state is flushed by each process itself.

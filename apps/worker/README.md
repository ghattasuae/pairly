# pairly-worker

Pairly background worker (`pairly_worker`), built on [arq](https://github.com/python-arq/arq) (MIT) with Valkey as the queue.

Run locally:

```sh
uv run arq pairly_worker.main.WorkerSettings          # the worker
uv run arq --check pairly_worker.main.WorkerSettings  # health check (exit 0 when healthy)
```

Settings come from the environment and have no defaults for secrets. Required:
`PAIRLY_DATABASE_URL` (non-owner app role) and `PAIRLY_REDIS_URL`. Missing either, the
worker and its health check refuse to start. Optional: `PAIRLY_AUDIT_VERIFY_HOUR` and
`PAIRLY_AUDIT_VERIFY_MINUTE` (UTC, default 02:30), `PAIRLY_WORKER_HEALTH_CHECK_INTERVAL`
(seconds, default 30), `PAIRLY_LOG_LEVEL`.

Jobs:

- `nightly_audit_verify` (cron) and `verify_audit_chains` (on demand): recompute every
  organisation's audit hash chain. A broken chain logs an `audit_chain_alert` error line
  and appends a `core.audit.chain_verification_failed` event to that organisation's log.

Jobs are serialised as JSON (never pickle). Anything that enqueues jobs must use the same
serializer (`pairly_worker.main.WorkerSettings.job_serializer`).

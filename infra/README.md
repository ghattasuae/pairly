# Infra

Local Docker Compose stack, Dockerfiles and init scripts. Alembic lives in `apps/api/alembic`.

## Run it

```bash
make dev                                   # default profile, waits until healthy
PROFILES="scan observability" make dev     # add ClamAV and LangFuse
make ps                                    # status and health
make logs SERVICE=api                      # follow one service
make down                                  # stop everything, keep volumes
```

`make dev` creates `.env` from `.env.example` on first run, with random secrets
(`scripts/gen_dev_env.py`). It never overwrites an existing `.env`. Compose refuses to
start if a required secret is missing.

## Services and profiles

| Profile | Services | Host ports (127.0.0.1 only) |
|---|---|---|
| default | postgres (pgvector), redis (Valkey 8.1), minio, minio-init (job), mailpit, migrate (job), api, worker | 5432, 6379, 9000, 9001, 1025, 8025, 8000 |
| `web` | web (Next.js) | 3100 (`PAIRLY_WEB_PORT`; the container listens on 3000) |
| `scan` | clamav | 3310 |
| `observability` | clickhouse, langfuse-worker, langfuse-web | 3200 (`LANGFUSE_HOST_PORT`) |

LangFuse reuses our Postgres (database `langfuse`, role `langfuse`), our Redis (database 1)
and our MinIO (bucket `langfuse`, its own access key). The `scan` and `observability`
profiles need more memory than a 2 GB Docker VM has: give Docker at least 4 GB for them.

## Database roles

`postgres/init/*.sql` runs once, on an empty volume:

- `pairly_owner` owns the database, the `public` schema and all Pairly tables. Only the
  `migrate` job connects as it.
- `pairly_app` is what the API and worker connect as. It is not an owner and has no
  `BYPASSRLS`, so Row Level Security applies. It has no default table rights: each
  migration grants exactly what it needs. It has no access to the `auth` schema.
- `pairly_auth` is what Better Auth (web) connects as. It owns only the `auth` schema
  (its search path), runs its own DDL there and has no rights on `public`.
- `langfuse` owns only the `langfuse` database.

Init scripts do not run again on an existing volume. After changing them or rotating
`.env`, reset the data: `docker compose --env-file .env -f infra/docker-compose.yml down -v`.

## Object storage

`minio/init.sh` creates the `pairly-documents` and `langfuse` buckets (private) and one
access key per consumer, each limited to its own bucket. Root credentials are used only by
that job.

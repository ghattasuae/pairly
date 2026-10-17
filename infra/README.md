# Infra

Two ways to run the stack locally. Alembic lives in `apps/api/alembic`.

- **Native (the default for development and tests)**: everything runs as local
  processes, no containers. `make dev`.
- **Docker Compose (for CI and self-hosters)**: this folder's `docker-compose.yml`,
  Dockerfiles and init scripts. `make docker-dev`.

Both read the repo-root `.env`, which `make env` (run by `make dev` and `make docker-dev`)
creates from `.env.example` with random secrets (`scripts/gen_dev_env.py`). It never
overwrites an existing `.env`; a missing secret stops either stack.

## Native stack

```bash
make dev            # same as make local-up
make local-status   # processes, /healthz, /readyz, web health
make local-logs SERVICE=worker
make local-test     # pytest against fresh test databases (ci-test without Docker)
make local-down     # stop everything (make down does the same)
```

Needs Postgres 16 with pgvector, Valkey and Tesseract installed natively (on macOS,
Homebrew `postgresql@16`, `pgvector`, `valkey`, `tesseract`; set `LOCAL_PG_BIN` and
`LOCAL_VALKEY_BIN` when they live elsewhere). All state lives in `.local/` (gitignored):

| Part | How it runs | Where |
|---|---|---|
| Postgres 16 + pgvector | `scripts/local_pg.sh`: a project cluster on 127.0.0.1:55432 (`LOCAL_PG_PORT`), shared_buffers 64MB, max_connections 60 | `.local/pg` |
| Valkey | `scripts/local_valkey.sh`: 127.0.0.1:56379 (`LOCAL_VALKEY_PORT`), password from `.env`, maxmemory 64mb, noeviction | `.local/valkey` |
| migrations | `alembic upgrade heads` as `pairly_owner`, on every `make dev` | |
| api | uvicorn on 127.0.0.1:8000 | `.local/logs/api.log` |
| worker | arq | `.local/logs/worker.log` |
| web | `next dev` on 127.0.0.1:3100 (`LOCAL_WEB_MODE=start` builds once and runs `next start`) | `.local/logs/web.log` |

`scripts/local_stack.sh` starts the api, worker and web at low priority (`nice -n 10`),
each in its own session with a pid file in `.local/logs`, and gives each one only the
variables it needs (as compose does). `make local-down` stops them, then Valkey and
Postgres, cleanly.

The first `scripts/local_pg.sh start` runs `postgres/init/*.sql`, the same files the
compose Postgres runs, with the passwords from `.env`, so roles and grants are identical.
It also keeps an unmigrated copy (`pairly_template`) that `make local-test` uses to
create fresh `pairly_test` and `pairly_scratch` databases on every run.
`scripts/local_pg.sh reset` deletes the cluster and starts over (needed after rotating
the database passwords in `.env`); `scripts/local_pg.sh env [dev|test]` prints the URLs.

Docker-only services are replaced by development backends behind the same interfaces.
Each is selected by a setting that production refuses (the settings fail to load outside
`PAIRLY_ENV=development` or `test`):

| Compose service | Native replacement | Setting |
|---|---|---|
| MinIO | Filesystem object store in `.local/objects/<bucket>/`: SHA-256 checked on write and on every read, atomic writes. Signed GET URLs (HMAC over key, expiry, type and disposition, key derived from `PAIRLY_S3_SECRET_KEY`) are served by `GET /v1/_dev/objects/...`, which the API mounts only in development and test with this store. Audit anchors are write once in code (exclusive create, read-only files, no delete). | `PAIRLY_OBJECT_STORE=filesystem`, `PAIRLY_OBJECT_STORE_DIR` |
| Mailpit | Email outbox in `.local/mail`: each message as `<id>.eml` and `<id>.json`, plus a line in `index.jsonl`, written by the worker and by the web app (Better Auth magic links and invitations) | `PAIRLY_EMAIL_TRANSPORT=outbox`, `PAIRLY_OUTBOX_DIR` |
| ClamAV | The fake scanner, recorded on each document as `pairly-dev-fake-scanner/1 (development only, EICAR only, not a virus scan)`. It detects the EICAR test string, so the infected path still works. It is not a virus scan; production needs clamd. | `PAIRLY_VIRUS_SCANNER=dev-fake` |

The admin targets run natively too: `make bootstrap-org`, `make demo-expense`,
`make dev-seed-approval` and `make audit-anchor-now`. Add `STACK=docker` to run them in
the compose stack instead.

## Docker Compose stack

For CI and self-hosters.

```bash
make docker-dev                                   # default profile, waits until healthy
PROFILES="scan observability" make docker-dev     # add ClamAV and LangFuse
make docker-ps                                    # status and health
make docker-logs SERVICE=api                      # follow one service
make docker-down                                  # stop everything, keep volumes
```

Compose refuses to start if a required secret is missing.

### Services and profiles

| Profile | Services | Host ports (127.0.0.1 only) |
|---|---|---|
| default | postgres (pgvector), redis (Valkey 8.1), minio, minio-init (job), mailpit, migrate (job), api, worker | 5432, 6379, 9000, 9001, 1025, 8025, 8000 |
| `web` | web (Next.js) | 3100 (`PAIRLY_WEB_PORT`; the container listens on 3000) |
| `scan` | clamav | 3310 |
| `observability` | clickhouse, langfuse-worker, langfuse-web | 3200 (`LANGFUSE_HOST_PORT`) |

LangFuse reuses our Postgres (database `langfuse`, role `langfuse`), our Redis (database 1)
and our MinIO (bucket `langfuse`, its own access key). The `scan` and `observability`
profiles need more memory than a 2 GB Docker VM has: give Docker at least 4 GB for them.

## Database roles (both stacks)

`postgres/init/*.sql` runs once, on an empty volume (compose) or a new cluster (native):

- `pairly_owner` owns the database, the `public` schema and all Pairly tables. Only the
  `migrate` job connects as it.
- `pairly_app` is what the API and worker connect as. It is not an owner and has no
  `BYPASSRLS`, so Row Level Security applies. It has no default table rights: each
  migration grants exactly what it needs. It has no access to the `auth` schema.
- `pairly_auth` is what Better Auth (web) connects as. It owns only the `auth` schema
  (its search path), runs its own DDL there and has no rights on `public`.
- `langfuse` owns only the `langfuse` database.

Init scripts do not run again on an existing volume or cluster. After changing them or
rotating `.env`, reset the data: `scripts/local_pg.sh reset` (native) or
`docker compose --env-file .env -f infra/docker-compose.yml down -v` (compose).

## Object storage (compose)

`minio/init.sh` creates the `pairly-documents` and `langfuse` buckets (private) and one
access key per consumer, each limited to its own bucket. Root credentials are used only by
that job.

## API tooling

- `bruno/` is a [Bruno](https://www.usebruno.com) collection of the main endpoints, with
  `native` and `docker` environments (secrets stay local). See `bruno/README.md`.
- `api-docs/vendor/redoc/` is the vendored Redoc bundle (MIT) that `make api-docs` embeds
  in `dist/api-docs/index.html`, next to `dist/api-docs/openapi.json`. The page fetches
  nothing at run time (its Content Security Policy forbids network access).

## Hosted demo (Fly.io)

`fly/` holds one `fly.toml` per app of the public demo (web, API, worker, Postgres with
pgvector, ClamAV, and the ops app that migrates, seeds and resets it every night), plus
`fly/db/Dockerfile`. The design, the cost estimate and the exact steps are in
[docs/hosted-demo.md](../docs/hosted-demo.md). Nothing is deployed from this repository
automatically.

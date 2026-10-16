# ADR 0007: A native local stack for development, Docker for CI and self-hosting

Status: accepted

Date: 2026-10-08

## Context

The first stack ran everything in Docker Compose. On the development machine, containers were slow and heavy next to the other work, and tests waited on them. CI and self-hosters still want one reproducible container setup.

## Decision

* Development and tests run natively (`make dev`, `make local-test`, `scripts/local_stack.sh`): Postgres 16 with pgvector and Valkey as local processes with their data in `.local/`, and the API, worker and web app as plain processes at low priority.
* Services that only exist as containers get development backends behind the same interfaces: a filesystem object store, an email outbox of files, and a fake virus scanner that only detects the EICAR test string. Each is selected by a setting that production refuses to load.
* Both stacks read the same `.env` and run the same database init scripts, so roles and grants are identical.
* Docker Compose (`make docker-dev`, `infra/docker-compose.yml`) stays for CI and self-hosting.

## Consequences

* `make local-test` creates fresh test databases from an unmigrated template on every run.
* The fake scanner is not a virus scan. Production needs ClamAV.
* Two ways to run means two to keep working. CI tests against a throwaway Postgres container with the same init scripts (`scripts/ci_postgres.sh`) and builds the Python image.

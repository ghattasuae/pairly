#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Throwaway Postgres for the test suite, in CI and locally.
#
# It runs the same image as infra/docker-compose.yml and copies infra/postgres/init into
# /docker-entrypoint-initdb.d, so the roles (pairly_owner, pairly_app, pairly_auth,
# langfuse) come from exactly the same SQL as the dev stack. Nothing is duplicated here.
# The files are copied (docker cp), not bind mounted, so this also works when the
# checkout is outside the folders Docker Desktop shares with its VM.
# Every password is random and lives only as long as the container.
#
# Usage:
#   scripts/ci_postgres.sh start   # start, wait until ready, print the env to use
#   scripts/ci_postgres.sh stop    # remove the container (data is not kept)
#
# `start` writes the test env to $GITHUB_ENV (and masks the secrets) when it runs in
# GitHub Actions. Elsewhere it prints `export` lines:
#   eval "$(scripts/ci_postgres.sh start)"
#   uv run pytest
#   scripts/ci_postgres.sh stop
#
# Settings (environment): CI_PG_IMAGE, CI_PG_PORT (default 55432), CI_PG_NAME.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep in step with the postgres service in infra/docker-compose.yml.
IMAGE="${CI_PG_IMAGE:-pgvector/pgvector:0.8.7-pg16-bookworm}"
PORT="${CI_PG_PORT:-55432}"
NAME="${CI_PG_NAME:-pairly-ci-postgres}"
DB="pairly"
SCRATCH_DB="pairly_scratch"

rand() { openssl rand -hex 24; }

log() { echo "ci_postgres: $*" >&2; }

start() {
  if docker container inspect "$NAME" >/dev/null 2>&1; then
    log "container $NAME already exists; run '$0 stop' first"
    exit 1
  fi

  local super_pw owner_pw app_pw auth_pw lf_pw
  super_pw="$(rand)"; owner_pw="$(rand)"; app_pw="$(rand)"; auth_pw="$(rand)"; lf_pw="$(rand)"

  # Same server settings as compose that matter for tests: scram auth everywhere.
  docker create --name "$NAME" \
    -p "127.0.0.1:${PORT}:5432" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD="$super_pw" \
    -e POSTGRES_DB="$DB" \
    -e POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256 --auth-local=scram-sha-256" \
    -e PAIRLY_OWNER_DB_PASSWORD="$owner_pw" \
    -e PAIRLY_APP_DB_PASSWORD="$app_pw" \
    -e PAIRLY_AUTH_DB_PASSWORD="$auth_pw" \
    -e LANGFUSE_DB_PASSWORD="$lf_pw" \
    -e TZ=UTC -e PGTZ=UTC \
    "$IMAGE" postgres -c password_encryption=scram-sha-256 >/dev/null
  docker cp "$ROOT/infra/postgres/init/." "$NAME:/docker-entrypoint-initdb.d/"
  docker start "$NAME" >/dev/null

  # TCP check on purpose: during init the server listens on the socket only, so this
  # succeeds only after every init script ran and the real server is up.
  local i
  for i in $(seq 1 60); do
    if docker exec "$NAME" pg_isready -h 127.0.0.1 -U postgres -d "$DB" >/dev/null 2>&1; then
      break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
      log "postgres exited during init; logs follow"
      docker logs "$NAME" >&2 || true
      exit 1
    fi
    if [ "$i" -eq 60 ]; then
      log "postgres did not become ready in time; logs follow"
      docker logs "$NAME" >&2 || true
      exit 1
    fi
    sleep 2
  done

  # Fail closed: prove the init SQL ran, instead of finding out from a confusing
  # authentication error later.
  local roles
  roles="$(docker exec -e PGPASSWORD="$super_pw" "$NAME" psql -U postgres -d "$DB" -tAc \
    "SELECT count(*) FROM pg_roles WHERE rolname IN ('pairly_owner', 'pairly_app', 'pairly_auth')")"
  if [ "$roles" != "3" ]; then
    log "init scripts did not create the Pairly roles (found $roles of 3); logs follow"
    docker logs "$NAME" >&2 || true
    exit 1
  fi
  # A second, dedicated database for tests that migrate down and up again (the 0008
  # data migration test). It is a copy of the freshly initialised database (roles,
  # extension and schema ownership, no tables yet), so such a test never touches the
  # shared test database, nor rows that earlier test sessions wrote with other keys.
  docker exec -e PGPASSWORD="$super_pw" "$NAME" psql -q -U postgres -d postgres \
    -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE ${SCRATCH_DB} TEMPLATE ${DB} OWNER pairly_owner" \
    -c "REVOKE ALL ON DATABASE ${SCRATCH_DB} FROM PUBLIC" >/dev/null

  local owner_url="postgresql://pairly_owner:${owner_pw}@127.0.0.1:${PORT}/${DB}"
  local scratch_url="postgresql://pairly_owner:${owner_pw}@127.0.0.1:${PORT}/${SCRATCH_DB}"
  local app_url="postgresql://pairly_app:${app_pw}@127.0.0.1:${PORT}/${DB}"

  # Ready inside the container is not enough: the published host port can lag behind
  # (Colima and Docker Desktop forward it asynchronously). Wait until the app role can
  # run SELECT 1 through 127.0.0.1:$PORT, exactly the way the tests connect. The URL goes
  # through the environment, never the command line.
  local tries=30
  for i in $(seq 1 "$tries"); do
    if PROBE_URL="$app_url" uv run --project "$ROOT" --frozen --no-sync python - \
      >/dev/null 2>&1 <<'PY'
import asyncio
import os

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(os.environ["PROBE_URL"], timeout=3)
    try:
        if await conn.fetchval("SELECT 1") != 1:
            raise SystemExit(1)
    finally:
        await conn.close()


asyncio.run(main())
PY
    then
      break
    fi
    if [ "$i" -eq "$tries" ]; then
      log "postgres is ready in $NAME, but 'SELECT 1' as pairly_app through" \
        "127.0.0.1:${PORT} still fails after ${tries} tries; check the port mapping"
      exit 1
    fi
    sleep 1
  done
  log "ready on 127.0.0.1:${PORT}"

  if [ "${GITHUB_ACTIONS:-}" = "true" ] && [ -n "${GITHUB_ENV:-}" ]; then
    local pw
    for pw in "$super_pw" "$owner_pw" "$app_pw" "$auth_pw" "$lf_pw"; do
      echo "::add-mask::$pw"
    done
    {
      echo "PAIRLY_DATABASE_URL_OWNER=$owner_url"
      echo "PAIRLY_TEST_DATABASE_URL_OWNER=$owner_url"
      echo "PAIRLY_TEST_DATABASE_URL=$app_url"
      echo "PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER=$scratch_url"
      echo "PAIRLY_DB_APP_ROLE=pairly_app"
    } >>"$GITHUB_ENV"
  else
    echo "export PAIRLY_DATABASE_URL_OWNER='$owner_url'"
    echo "export PAIRLY_TEST_DATABASE_URL_OWNER='$owner_url'"
    echo "export PAIRLY_TEST_DATABASE_URL='$app_url'"
    echo "export PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER='$scratch_url'"
    echo "export PAIRLY_DB_APP_ROLE=pairly_app"
  fi
}

stop() {
  docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  log "removed $NAME"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

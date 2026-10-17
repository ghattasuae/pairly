#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
#
# Project-local Postgres 16 cluster for the native stack (no containers).
#
#   scripts/local_pg.sh start            # init on first run, then start (waits until ready)
#   scripts/local_pg.sh stop             # clean shutdown (fast mode: ends sessions, no data loss)
#   scripts/local_pg.sh status           # running or not, and where
#   scripts/local_pg.sh reset            # stop, delete the cluster, init and start again
#   scripts/local_pg.sh env [dev|test]   # print `export` lines for the dev or test databases
#   scripts/local_pg.sh fresh-test-dbs   # recreate pairly_test and pairly_scratch, empty
#
# Data lives in .local/pg (gitignored), the server listens on 127.0.0.1:$LOCAL_PG_PORT
# (default 55432) and on a socket inside .local/pg/run. Small memory settings
# (shared_buffers 64MB, max_connections 60), as in infra/docker-compose.yml.
#
# First start runs infra/postgres/init/*.sql, the same files the compose Postgres runs,
# with the passwords from .env, so the roles and grants are identical (pairly_owner,
# pairly_app, pairly_auth, langfuse; vector extension; schemas `public` and `auth`).
# It then copies that fresh database, before any migration, to `pairly_template`, which
# the test databases are made from:
#   pairly_test     what `make local-test` migrates and tests (fresh on every run)
#   pairly_scratch  for tests that migrate down and up again (owner only)
set -euo pipefail

# shellcheck source=scripts/local_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/local_lib.sh"

PG_DIR="$LOCAL_DIR/pg"
DATA="$PG_DIR/data"
RUN="$PG_DIR/run"
LOGFILE="$PG_DIR/postgres.log"
MARKER="$PG_DIR/initialised"
TEMPLATE_DB="pairly_template"
TEST_DB="pairly_test"
SCRATCH_DB="pairly_scratch"

for bin in initdb pg_ctl psql pg_isready; do
  [ -x "$LOCAL_PG_BIN/$bin" ] || local_die "$LOCAL_PG_BIN/$bin not found (set LOCAL_PG_BIN to a Postgres 16 bin folder)"
done

local_settings

superuser_psql() { # database, then psql arguments
  local db="$1"
  shift
  PGPASSWORD="$POSTGRES_PASSWORD" "$LOCAL_PG_BIN/psql" -X -q -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -p "$LOCAL_PG_PORT" -U postgres -d "$db" "$@"
}

running() {
  "$LOCAL_PG_BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1
}

server_start() {
  mkdir -p "$RUN"
  chmod 700 "$PG_DIR" "$RUN"
  # nice: the cluster is a background helper and must not heat the machine.
  nice -n 10 "$LOCAL_PG_BIN/pg_ctl" -D "$DATA" -l "$LOGFILE" -w -t 60 \
    -o "-p $LOCAL_PG_PORT -k $RUN -c listen_addresses=127.0.0.1" start >/dev/null ||
    { tail -n 30 "$LOGFILE" >&2; local_die "postgres did not start; log: $LOGFILE"; }
  local i
  for i in $(seq 1 30); do
    if "$LOCAL_PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$LOCAL_PG_PORT"; then
      return 0
    fi
    sleep 1
  done
  local_die "postgres started but is not accepting connections on 127.0.0.1:$LOCAL_PG_PORT"
}

init_cluster() {
  local_require POSTGRES_PASSWORD PAIRLY_OWNER_DB_PASSWORD PAIRLY_APP_DB_PASSWORD \
    PAIRLY_AUTH_DB_PASSWORD LANGFUSE_DB_PASSWORD
  local version
  version="$("$LOCAL_PG_BIN/postgres" --version | sed -E 's/.* ([0-9]+)\..*/\1/')"
  [ "$version" = "16" ] || local_die "need Postgres 16, $LOCAL_PG_BIN has $version"

  local_log "initialising a new cluster in $DATA"
  rm -rf "$DATA" "$MARKER"
  mkdir -p "$PG_DIR"
  chmod 700 "$PG_DIR"
  local pwfile
  pwfile="$(mktemp "$PG_DIR/.pw.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -f '$pwfile'" EXIT
  printf '%s\n' "$POSTGRES_PASSWORD" >"$pwfile"
  TZ=UTC "$LOCAL_PG_BIN/initdb" -D "$DATA" -U postgres --pwfile="$pwfile" \
    --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
    -E UTF8 --locale=C --no-instructions >"$PG_DIR/initdb.log" 2>&1 ||
    { cat "$PG_DIR/initdb.log" >&2; local_die "initdb failed"; }
  rm -f "$pwfile"
  trap - EXIT

  # Same server settings as the compose service (infra/docker-compose.yml).
  cat >>"$DATA/postgresql.conf" <<'CONF'

# ---- Pairly native stack (scripts/local_pg.sh). Port and socket come from pg_ctl -o.
shared_buffers = 64MB
effective_cache_size = 160MB
maintenance_work_mem = 32MB
work_mem = 2MB
max_connections = 60
password_encryption = 'scram-sha-256'
timezone = 'UTC'
log_timezone = 'UTC'
CONF

  server_start
  # The compose image creates $POSTGRES_DB before the init scripts; do the same, then run
  # the very same init SQL with the passwords in its environment (psql \getenv).
  superuser_psql postgres -c "CREATE DATABASE \"$POSTGRES_DB\""
  local sql
  for sql in "$LOCAL_ROOT"/infra/postgres/init/*.sql; do
    POSTGRES_DB="$POSTGRES_DB" \
      PAIRLY_OWNER_DB_PASSWORD="$PAIRLY_OWNER_DB_PASSWORD" \
      PAIRLY_APP_DB_PASSWORD="$PAIRLY_APP_DB_PASSWORD" \
      PAIRLY_AUTH_DB_PASSWORD="$PAIRLY_AUTH_DB_PASSWORD" \
      LANGFUSE_DB_PASSWORD="$LANGFUSE_DB_PASSWORD" \
      superuser_psql "$POSTGRES_DB" -f "$sql" >/dev/null ||
      local_die "init script $(basename "$sql") failed"
  done
  # Fail closed: prove the init SQL ran.
  local roles
  roles="$(superuser_psql "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM pg_roles WHERE rolname IN ('pairly_owner', 'pairly_app', 'pairly_auth')")"
  [ "$roles" = "3" ] || local_die "init scripts did not create the Pairly roles (found $roles of 3)"
  # The freshly initialised database, before any migration: the test databases' template.
  superuser_psql postgres \
    -c "CREATE DATABASE $TEMPLATE_DB TEMPLATE \"$POSTGRES_DB\" OWNER pairly_owner" \
    -c "REVOKE ALL ON DATABASE $TEMPLATE_DB FROM PUBLIC" \
    -c "ALTER DATABASE $TEMPLATE_DB WITH IS_TEMPLATE true ALLOW_CONNECTIONS false"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$MARKER"
  fresh_test_dbs
  local_log "cluster ready: roles, vector, schemas; databases $POSTGRES_DB, $TEST_DB, $SCRATCH_DB"
}

fresh_test_dbs() {
  # Same shape as scripts/ci_postgres.sh: an app-role database and an owner-only scratch
  # copy, both from the freshly initialised (unmigrated) template.
  superuser_psql postgres \
    -c "SET client_min_messages = warning" \
    -c "DROP DATABASE IF EXISTS $TEST_DB WITH (FORCE)" \
    -c "DROP DATABASE IF EXISTS $SCRATCH_DB WITH (FORCE)" \
    -c "CREATE DATABASE $TEST_DB TEMPLATE $TEMPLATE_DB OWNER pairly_owner" \
    -c "REVOKE ALL ON DATABASE $TEST_DB FROM PUBLIC" \
    -c "GRANT CONNECT ON DATABASE $TEST_DB TO pairly_app, pairly_auth" \
    -c "CREATE DATABASE $SCRATCH_DB TEMPLATE $TEMPLATE_DB OWNER pairly_owner" \
    -c "REVOKE ALL ON DATABASE $SCRATCH_DB FROM PUBLIC"
}

start() {
  if running; then
    [ -f "$MARKER" ] || local_die "running, but its first initialisation did not finish; run '$0 reset'"
    local_log "already running on 127.0.0.1:$LOCAL_PG_PORT"
    return 0
  fi
  if [ ! -f "$MARKER" ]; then
    init_cluster
    return 0
  fi
  server_start
  local_log "running on 127.0.0.1:$LOCAL_PG_PORT (data $DATA)"
}

stop() {
  if ! running; then
    local_log "not running"
    return 0
  fi
  "$LOCAL_PG_BIN/pg_ctl" -D "$DATA" -m fast -w -t 60 stop >/dev/null
  local_log "stopped"
}

status() {
  if running; then
    echo "postgres: running on 127.0.0.1:$LOCAL_PG_PORT (pid $(head -n 1 "$DATA/postmaster.pid"), data $DATA)"
  elif [ -f "$MARKER" ]; then
    echo "postgres: stopped (data $DATA)"
    return 3
  else
    echo "postgres: not initialised (run: scripts/local_pg.sh start)"
    return 3
  fi
}

print_env() {
  local target="${1:-dev}" db
  case "$target" in
    dev)
      db="$POSTGRES_DB"
      echo "export PAIRLY_DATABASE_URL='$(local_db_url pairly_app "$PAIRLY_APP_DB_PASSWORD" "$db")'"
      echo "export PAIRLY_DATABASE_URL_OWNER='$(local_db_url pairly_owner "$PAIRLY_OWNER_DB_PASSWORD" "$db")'"
      echo "export PAIRLY_AUTH_DATABASE_URL='$(local_db_url pairly_auth "$PAIRLY_AUTH_DB_PASSWORD" "$db")'"
      ;;
    test)
      # The variables scripts/ci_postgres.sh exports, for the local test databases.
      local owner app scratch
      owner="$(local_db_url pairly_owner "$PAIRLY_OWNER_DB_PASSWORD" "$TEST_DB")"
      app="$(local_db_url pairly_app "$PAIRLY_APP_DB_PASSWORD" "$TEST_DB")"
      scratch="$(local_db_url pairly_owner "$PAIRLY_OWNER_DB_PASSWORD" "$SCRATCH_DB")"
      echo "export PAIRLY_DATABASE_URL_OWNER='$owner'"
      echo "export PAIRLY_TEST_DATABASE_URL_OWNER='$owner'"
      echo "export PAIRLY_TEST_DATABASE_URL='$app'"
      echo "export PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER='$scratch'"
      echo "export PAIRLY_DB_APP_ROLE=pairly_app"
      ;;
    *) local_die "usage: $0 env [dev|test]" ;;
  esac
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  reset)
    stop
    rm -rf "$PG_DIR"
    local_log "deleted $PG_DIR"
    start
    ;;
  env) print_env "${2:-dev}" ;;
  fresh-test-dbs)
    running || local_die "not running (scripts/local_pg.sh start)"
    fresh_test_dbs
    local_log "recreated $TEST_DB and $SCRATCH_DB"
    ;;
  *)
    echo "usage: $0 start|stop|status|reset|env [dev|test]|fresh-test-dbs" >&2
    exit 2
    ;;
esac

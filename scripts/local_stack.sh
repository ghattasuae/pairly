#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# The native local stack: everything runs as plain processes on this machine, no
# containers. `make dev` / `make local-up` and friends call this.
#
#   scripts/local_stack.sh up        # Postgres, Valkey, migrations, then api, worker, web
#   scripts/local_stack.sh down      # stop web, worker, api, Valkey and Postgres
#   scripts/local_stack.sh status    # what runs, and the health of each part
#   scripts/local_stack.sh test      # pytest against fresh local test databases
#   scripts/local_stack.sh migrate   # alembic upgrade heads (owner role)
#   scripts/local_stack.sh logs [api|worker|web]
#   scripts/local_stack.sh exec owner|app|worker|demo -- CMD...   # CMD with that process's env
#
# What replaces the Docker-only services (development and test only; the settings refuse
# each of them in production):
#   MinIO    filesystem object store in .local/objects, signed URLs served by the API
#            (/v1/_dev/objects/...), write-once audit anchors
#   Mailpit  email outbox in .local/mail (.eml + JSON + index.jsonl), web and worker
#   ClamAV   the dev-fake scanner (EICAR test string only, labelled on every document)
#
# Processes run at low priority (nice 10), each in its own session, with pid files and
# logs in .local/logs. Settings (environment): LOCAL_WEB_MODE=dev (next dev, default) or
# start (next build once, then next start; LOCAL_WEB_BUILD=1 forces a rebuild), and those
# of scripts/local_lib.sh.
set -euo pipefail

# shellcheck source=scripts/local_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/local_lib.sh"

SCRIPTS="$LOCAL_ROOT/scripts"
VENV_BIN="$LOCAL_ROOT/.venv/bin"
WEB_DIR="$LOCAL_ROOT/apps/web"
WEB_MODE="${LOCAL_WEB_MODE:-dev}"
SERVICES="api worker web"

local_settings

pidfile() { echo "$LOCAL_LOGS/$1.pid"; }
logfile() { echo "$LOCAL_LOGS/$1.log"; }

# The pid of a running service, only if it still leads its own process group (so a
# reused pid of some other program is never signalled).
service_pid() {
  local file p pgid
  file="$(pidfile "$1")"
  [ -f "$file" ] || return 1
  p="$(cat "$file")"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null || return 1
  pgid="$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')"
  [ "$pgid" = "$p" ] || return 1
  echo "$p"
}

# True while the launched process lives (during startup it may not lead its group yet).
alive() {
  local file
  file="$(pidfile "$1")"
  [ -f "$file" ] && kill -0 "$(cat "$file")" 2>/dev/null
}

# launch NAME DIR CMD...: start CMD in DIR with exactly $LOCAL_ENV, nice 10, in a new
# session (its own process group, so `down` stops its children too).
launch() {
  local name="$1" dir="$2"
  shift 2
  mkdir -p "$LOCAL_LOGS"
  local log
  log="$(logfile "$name")"
  echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) starting $name" >>"$log"
  (
    cd "$dir"
    exec nice -n 10 env -i "${LOCAL_ENV[@]}" \
      perl -MPOSIX -e 'POSIX::setsid() or die "setsid: $!\n"; exec { $ARGV[0] } @ARGV or die "exec: $!\n"' \
      "$@"
  ) >>"$log" 2>&1 </dev/null &
  echo "$!" >"$(pidfile "$name")"
}

wait_http() { # name url seconds
  local i
  for i in $(seq 1 "$3"); do
    if curl -fsS -o /dev/null --max-time 5 "$2" 2>/dev/null; then
      return 0
    fi
    alive "$1" || { tail -n 40 "$(logfile "$1")" >&2; local_die "$1 exited; log: $(logfile "$1")"; }
    sleep 1
  done
  tail -n 40 "$(logfile "$1")" >&2
  local_die "$1 did not answer $2 in $3 seconds; log: $(logfile "$1")"
}

need_tools() {
  [ -x "$VENV_BIN/uvicorn" ] && [ -x "$VENV_BIN/arq" ] && [ -x "$VENV_BIN/alembic" ] ||
    local_die "no Python environment in .venv; run 'make install'"
  [ -x "$WEB_DIR/node_modules/.bin/next" ] || local_die "no web dependencies; run 'make install'"
  command -v curl >/dev/null || local_die "curl is needed"
}

migrate() {
  local_owner_env
  env -i "${LOCAL_ENV[@]}" nice -n 10 "$VENV_BIN/alembic" -c "$LOCAL_ROOT/apps/api/alembic.ini" upgrade heads
}

start_api() {
  service_pid api >/dev/null && { local_log "api already running"; return 0; }
  local_app_env
  launch api "$LOCAL_ROOT" "$VENV_BIN/uvicorn" pairly_api.main:app \
    --host 127.0.0.1 --port "$LOCAL_API_PORT" --no-server-header
  wait_http api "http://127.0.0.1:$LOCAL_API_PORT/healthz" 90
  local_log "api on http://127.0.0.1:$LOCAL_API_PORT"
}

start_worker() {
  service_pid worker >/dev/null && { local_log "worker already running"; return 0; }
  local_worker_env
  local log mark i
  log="$(logfile worker)"
  mark=0
  [ -f "$log" ] && mark="$(wc -l <"$log" | tr -d ' ')"
  launch worker "$LOCAL_ROOT" "$VENV_BIN/arq" pairly_worker.main.WorkerSettings
  for i in $(seq 1 90); do
    if tail -n "+$((mark + 1))" "$log" 2>/dev/null | grep -q "worker_startup"; then
      local_log "worker running (arq)"
      return 0
    fi
    alive worker || { tail -n 40 "$log" >&2; local_die "worker exited; log: $log"; }
    sleep 1
  done
  tail -n 40 "$log" >&2
  local_die "worker did not finish starting in 90 seconds; log: $log"
}

start_web() {
  service_pid web >/dev/null && { local_log "web already running"; return 0; }
  local next="$WEB_DIR/node_modules/.bin/next"
  (cd "$WEB_DIR" && nice -n 10 node scripts/copy-pdfjs.mjs >/dev/null)
  case "$WEB_MODE" in
    dev)
      local_web_env dev
      launch web "$WEB_DIR" "$next" dev --port "$LOCAL_WEB_PORT" --hostname 127.0.0.1
      ;;
    start)
      if [ ! -f "$WEB_DIR/.next/BUILD_ID" ] || [ "${LOCAL_WEB_BUILD:-0}" = "1" ]; then
        local_log "building the web app (next build), once"
        (cd "$WEB_DIR" && env -i "PATH=$PATH" "HOME=$HOME" NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 \
          nice -n 10 "$next" build) >>"$(logfile web-build)" 2>&1 ||
          { tail -n 40 "$(logfile web-build)" >&2; local_die "next build failed; log: $(logfile web-build)"; }
      fi
      local_web_env start
      launch web "$WEB_DIR" "$next" start --port "$LOCAL_WEB_PORT" --hostname 127.0.0.1
      ;;
    *) local_die "LOCAL_WEB_MODE must be dev or start" ;;
  esac
  # next dev compiles on the first request, so allow it time.
  wait_http web "http://127.0.0.1:$LOCAL_WEB_PORT/api/healthz" 240
  local_log "web on http://localhost:$LOCAL_WEB_PORT ($WEB_MODE)"
}

stop_service() {
  local name="$1" p i
  if ! p="$(service_pid "$name")"; then
    rm -f "$(pidfile "$name")"
    return 0
  fi
  kill -TERM -- "-$p" 2>/dev/null || true
  for i in $(seq 1 80); do
    kill -0 -- "-$p" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 -- "-$p" 2>/dev/null; then
    local_log "$name did not stop in 20 seconds; killing it"
    kill -KILL -- "-$p" 2>/dev/null || true
  fi
  rm -f "$(pidfile "$name")"
  local_log "stopped $name"
}

up() {
  need_tools
  local_require_app_secrets
  mkdir -p "$LOCAL_LOGS" "$LOCAL_DIR/objects" "$LOCAL_DIR/mail"
  chmod 700 "$LOCAL_DIR"
  "$SCRIPTS/local_pg.sh" start
  "$SCRIPTS/local_valkey.sh" start
  migrate
  start_api
  start_worker
  start_web
  echo ""
  echo "Pairly is up (native). Web http://localhost:$LOCAL_WEB_PORT  API http://localhost:$LOCAL_API_PORT"
  echo "Email outbox: $LOCAL_DIR/mail  Logs: $LOCAL_LOGS  Stop: make local-down"
}

down() {
  local name
  for name in web worker api; do
    stop_service "$name"
  done
  "$SCRIPTS/local_valkey.sh" stop
  "$SCRIPTS/local_pg.sh" stop
}

status() {
  local name p rc=0
  "$SCRIPTS/local_pg.sh" status || rc=1
  "$SCRIPTS/local_valkey.sh" status || rc=1
  for name in $SERVICES; do
    if p="$(service_pid "$name")"; then
      echo "$name: running (pid $p, log $(logfile "$name"))"
    else
      echo "$name: stopped"
      rc=1
    fi
  done
  if service_pid api >/dev/null; then
    echo "api /healthz: $(curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_API_PORT/healthz" 2>&1 || echo failed)"
    echo "api /readyz:  $(curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_API_PORT/readyz" 2>&1 || echo failed)"
  fi
  if service_pid web >/dev/null; then
    echo "web /api/healthz: $(curl -fsS --max-time 10 "http://127.0.0.1:$LOCAL_WEB_PORT/api/healthz" 2>&1 || echo failed)"
  fi
  return "$rc"
}

run_tests() {
  local started=0 rc=0
  if ! "$SCRIPTS/local_pg.sh" status >/dev/null 2>&1; then
    "$SCRIPTS/local_pg.sh" start
    started=1
  fi
  # Same steps as `make ci-test`, on fresh, empty test databases.
  "$SCRIPTS/local_pg.sh" fresh-test-dbs
  eval "$("$SCRIPTS/local_pg.sh" env test)"
  (
    cd "$LOCAL_ROOT"
    nice -n 10 uv run --frozen alembic -c apps/api/alembic.ini upgrade heads &&
      nice -n 10 uv run --frozen alembic -c apps/api/alembic.ini check &&
      nice -n 10 uv run --frozen pytest --cov --cov-report=term-missing "$@"
  ) || rc=$?
  if [ "$started" = 1 ]; then
    "$SCRIPTS/local_pg.sh" stop
  fi
  return "$rc"
}

exec_with() {
  local kind="$1"
  shift
  [ "${1:-}" = "--" ] && shift
  [ "$#" -gt 0 ] || local_die "usage: $0 exec owner|app|worker|demo -- CMD..."
  case "$kind" in
    owner) local_owner_env ;;
    app) local_app_env ;;
    worker) local_worker_env ;;
    demo) local_demo_env ;;
    *) local_die "usage: $0 exec owner|app|worker|demo -- CMD..." ;;
  esac
  # Extra variables for this command only, e.g. PAIRLY_DEVSEED=1 (LOCAL_EXTRA_ENV).
  if [ -n "${LOCAL_EXTRA_ENV:-}" ]; then
    local extra
    for extra in $LOCAL_EXTRA_ENV; do LOCAL_ENV+=("$extra"); done
  fi
  cd "$LOCAL_ROOT"
  exec env -i "${LOCAL_ENV[@]}" "PATH=$VENV_BIN:$PATH" "$@"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  test)
    shift
    run_tests "$@"
    ;;
  migrate) migrate ;;
  logs)
    shift
    mkdir -p "$LOCAL_LOGS"
    if [ -n "${1:-}" ]; then tail -n 200 -f "$(logfile "$1")"; else tail -n 50 -f "$LOCAL_LOGS"/*.log; fi
    ;;
  exec)
    shift
    exec_with "$@"
    ;;
  *)
    echo "usage: $0 up|down|status|test|migrate|logs [service]|exec owner|app|worker|demo -- CMD..." >&2
    exit 2
    ;;
esac

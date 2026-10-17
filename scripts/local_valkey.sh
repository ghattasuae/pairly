#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
#
# Project-local Valkey (the arq queue) for the native stack (no containers).
#
#   scripts/local_valkey.sh start    # start in the background, wait until it answers
#   scripts/local_valkey.sh stop     # clean shutdown (saves the append-only file)
#   scripts/local_valkey.sh status
#
# Bound to 127.0.0.1:$LOCAL_VALKEY_PORT (default 56379), password REDIS_PASSWORD from
# .env, data in .local/valkey, maxmemory 64mb with noeviction (a full queue refuses
# writes instead of dropping jobs), append-only persistence, as in docker-compose.yml.
# The password goes into a 0600 config file, never onto a command line.
set -euo pipefail

# shellcheck source=scripts/local_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/local_lib.sh"

VK_DIR="$LOCAL_DIR/valkey"
CONF="$VK_DIR/valkey.conf"
PIDFILE="$VK_DIR/valkey.pid"
LOGFILE="$VK_DIR/valkey.log"

for bin in valkey-server valkey-cli; do
  [ -x "$LOCAL_VALKEY_BIN/$bin" ] || local_die "$LOCAL_VALKEY_BIN/$bin not found (set LOCAL_VALKEY_BIN)"
done

local_settings
local_require REDIS_PASSWORD

cli() {
  REDISCLI_AUTH="$REDIS_PASSWORD" "$LOCAL_VALKEY_BIN/valkey-cli" -h 127.0.0.1 -p "$LOCAL_VALKEY_PORT" "$@"
}

pid() {
  [ -f "$PIDFILE" ] || return 1
  local p
  p="$(cat "$PIDFILE")"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null && echo "$p"
}

write_conf() {
  mkdir -p "$VK_DIR"
  chmod 700 "$VK_DIR"
  local tmp
  tmp="$(mktemp "$VK_DIR/.conf.XXXXXX")"
  chmod 600 "$tmp"
  cat >"$tmp" <<CONF
# Written by scripts/local_valkey.sh on every start. Holds the password: mode 0600.
bind 127.0.0.1
protected-mode yes
port $LOCAL_VALKEY_PORT
daemonize yes
pidfile $PIDFILE
logfile $LOGFILE
dir $VK_DIR
appendonly yes
maxmemory 64mb
maxmemory-policy noeviction
requirepass "$REDIS_PASSWORD"
CONF
  mv "$tmp" "$CONF"
}

start() {
  if pid >/dev/null; then
    local_log "already running on 127.0.0.1:$LOCAL_VALKEY_PORT"
    return 0
  fi
  write_conf
  nice -n 10 "$LOCAL_VALKEY_BIN/valkey-server" "$CONF"
  local i
  for i in $(seq 1 30); do
    if [ "$(cli ping 2>/dev/null || true)" = "PONG" ]; then
      local_log "running on 127.0.0.1:$LOCAL_VALKEY_PORT (data $VK_DIR)"
      return 0
    fi
    sleep 0.5
  done
  tail -n 20 "$LOGFILE" >&2 || true
  local_die "valkey did not answer on 127.0.0.1:$LOCAL_VALKEY_PORT; log: $LOGFILE"
}

stop() {
  local p
  if ! p="$(pid)"; then
    local_log "not running"
    rm -f "$PIDFILE"
    return 0
  fi
  cli shutdown >/dev/null 2>&1 || kill -TERM "$p" 2>/dev/null || true
  local i
  for i in $(seq 1 40); do
    kill -0 "$p" 2>/dev/null || { local_log "stopped"; rm -f "$PIDFILE"; return 0; }
    sleep 0.25
  done
  local_die "valkey (pid $p) did not stop"
}

status() {
  local p
  if p="$(pid)"; then
    echo "valkey: running on 127.0.0.1:$LOCAL_VALKEY_PORT (pid $p, data $VK_DIR)"
  else
    echo "valkey: stopped"
    return 3
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *)
    echo "usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac

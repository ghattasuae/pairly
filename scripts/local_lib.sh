# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Shared settings for the native local stack (no containers). Sourced by
# scripts/local_pg.sh, scripts/local_valkey.sh and scripts/local_stack.sh; not run on
# its own. Written for bash 3.2 (the macOS /bin/bash).
#
# Everything lives under .local/ (gitignored):
#   .local/pg       Postgres 16 cluster (data, socket, log)
#   .local/valkey   Valkey data, config and log
#   .local/objects  filesystem object store (one folder per bucket)
#   .local/mail     development email outbox (index.jsonl, <id>.json, <id>.eml)
#   .local/logs     api, worker and web logs and pid files
#
# Settings (environment, all optional):
#   LOCAL_PG_PORT (55432), LOCAL_VALKEY_PORT (56379), LOCAL_PG_BIN, LOCAL_VALKEY_BIN,
#   PAIRLY_LOCAL_DIR (.local). Secrets and the rest come from the repo-root .env
#   (`make env`); a variable already set in the environment wins over .env.

LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${PAIRLY_LOCAL_DIR:-$LOCAL_ROOT/.local}"
LOCAL_PG_BIN="${LOCAL_PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
LOCAL_VALKEY_BIN="${LOCAL_VALKEY_BIN:-/opt/homebrew/opt/valkey/bin}"
LOCAL_PG_PORT="${LOCAL_PG_PORT:-55432}"
LOCAL_VALKEY_PORT="${LOCAL_VALKEY_PORT:-56379}"
LOCAL_LOGS="$LOCAL_DIR/logs"

local_log() { echo "$(basename "$0"): $*" >&2; }
local_die() { local_log "$*"; exit 1; }

# Read the repo-root .env into shell variables (not exported: each process gets an
# explicit list, see local_*_env). A variable already set wins. Values may be quoted.
local_load_dotenv() {
  local file="$LOCAL_ROOT/.env" line key val isset
  [ -f "$file" ] || local_die "no .env; run 'make env' first"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    line="${line#export }"
    key="${line%%=*}"
    val="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "$val" == \"*\" && ${#val} -ge 2 ]] || [[ "$val" == \'*\' && ${#val} -ge 2 ]]; then
      val="${val:1:${#val}-2}"
    fi
    eval "isset=\${$key+x}"
    if [ -z "$isset" ]; then
      printf -v "$key" '%s' "$val"
    fi
  done <"$file"
}

# Fail closed: name every required secret that is missing or empty.
local_require() {
  local name missing=""
  for name in "$@"; do
    eval "[ -n \"\${$name:-}\" ]" || missing="$missing $name"
  done
  [ -z "$missing" ] || local_die "missing in .env:$missing (run 'make env')"
}

# Percent-encode a value for a URL (passwords are hex, this is a safety net).
local_urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

local_db_url() { # role password database [scheme]
  local scheme="${4:-postgresql}"
  echo "${scheme}://$1:$(local_urlencode "$2")@127.0.0.1:${LOCAL_PG_PORT}/$3"
}

# Values derived once .env is loaded.
local_settings() {
  local_load_dotenv
  POSTGRES_DB="${POSTGRES_DB:-pairly}"
  LOCAL_API_PORT="${PAIRLY_API_PORT:-8000}"
  LOCAL_WEB_PORT="${PAIRLY_WEB_PORT:-3100}"
  PAIRLY_PUBLIC_API_URL="${PAIRLY_PUBLIC_API_URL:-http://localhost:${LOCAL_API_PORT}}"
  PAIRLY_PUBLIC_WEB_URL="${PAIRLY_PUBLIC_WEB_URL:-http://localhost:${LOCAL_WEB_PORT}}"
  # Tesseract may lack Arabic on a developer machine; OCR then runs in English only.
  LOCAL_OCR_LANGUAGES="${PAIRLY_OCR_LANGUAGES:-eng+ara}"
  if command -v tesseract >/dev/null 2>&1 && ! tesseract --list-langs 2>/dev/null | grep -qx ara; then
    LOCAL_OCR_LANGUAGES="eng"
  fi
}

# ---------------------------------------------------------------- per-process env
# Each process starts from an empty environment (env -i) plus exactly these variables,
# like the compose services: only the secrets it needs.

local_base_env() {
  LOCAL_ENV=(
    "PATH=$PATH"
    "HOME=$HOME"
    "LANG=${LANG:-en_US.UTF-8}"
    "TMPDIR=${TMPDIR:-/tmp}"
    "TZ=UTC"
    "PYTHONUNBUFFERED=1"
  )
}

# The API and the worker (x-app-env in infra/docker-compose.yml, with native backends).
local_app_env() {
  local_base_env
  local origin
  origin="$(python3 -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(f"{p.scheme}://{p.netloc}")' "$PAIRLY_PUBLIC_API_URL")"
  LOCAL_ENV+=(
    "PAIRLY_ENV=${PAIRLY_ENV:-development}"
    "PAIRLY_LOG_LEVEL=${PAIRLY_LOG_LEVEL:-INFO}"
    "PAIRLY_MODEL_MODE=${PAIRLY_MODEL_MODE:-recorded}"
    "PAIRLY_LLM_MONTHLY_BUDGET_USD=${PAIRLY_LLM_MONTHLY_BUDGET_USD:-20}"
    "PAIRLY_PUBLIC_API_URL=$PAIRLY_PUBLIC_API_URL"
    "PAIRLY_PUBLIC_WEB_URL=$PAIRLY_PUBLIC_WEB_URL"
    "PAIRLY_DATABASE_URL=$(local_db_url pairly_app "$PAIRLY_APP_DB_PASSWORD" "$POSTGRES_DB" postgresql+asyncpg)"
    "PAIRLY_REDIS_URL=redis://:$(local_urlencode "$REDIS_PASSWORD")@127.0.0.1:${LOCAL_VALKEY_PORT}/0"
    # Filesystem object store instead of MinIO (development and test only).
    "PAIRLY_OBJECT_STORE=filesystem"
    "PAIRLY_OBJECT_STORE_DIR=$LOCAL_DIR/objects"
    "PAIRLY_S3_BUCKET=${PAIRLY_S3_BUCKET:-pairly-documents}"
    "PAIRLY_S3_ACCESS_KEY=${PAIRLY_S3_ACCESS_KEY:-pairly-app}"
    "PAIRLY_S3_SECRET_KEY=$PAIRLY_S3_SECRET_KEY"
    # Email to files instead of Mailpit (development and test only).
    "PAIRLY_EMAIL_TRANSPORT=outbox"
    "PAIRLY_OUTBOX_DIR=$LOCAL_DIR/mail"
    "PAIRLY_SMTP_FROM=${PAIRLY_SMTP_FROM:-Pairly <no-reply@pairly.localhost>}"
    # The fake scanner instead of ClamAV: EICAR only, not a virus scan.
    "PAIRLY_VIRUS_SCANNER=dev-fake"
    "PAIRLY_INBOUND_EMAIL_DOMAIN=${PAIRLY_INBOUND_EMAIL_DOMAIN:-inbound.pairly.localhost}"
    "PAIRLY_MAX_UPLOAD_BYTES=${PAIRLY_MAX_UPLOAD_BYTES:-26214400}"
    "PAIRLY_SIGNED_URL_TTL_SECONDS=${PAIRLY_SIGNED_URL_TTL_SECONDS:-60}"
    "PAIRLY_PARSE_CONCURRENCY=${PAIRLY_PARSE_CONCURRENCY:-1}"
    "PAIRLY_PARSE_MEMORY_MB=${PAIRLY_PARSE_MEMORY_MB:-512}"
    "PAIRLY_OCR_LANGUAGES=$LOCAL_OCR_LANGUAGES"
    "PAIRLY_AUTH_JWKS_URL=http://127.0.0.1:${LOCAL_WEB_PORT}/api/auth/jwks"
    "PAIRLY_AUTH_ISSUER=${PAIRLY_AUTH_ISSUER:-$PAIRLY_PUBLIC_WEB_URL}"
    "PAIRLY_AUTH_AUDIENCE=${PAIRLY_AUTH_AUDIENCE:-$PAIRLY_PUBLIC_WEB_URL}"
    "PAIRLY_CORS_ORIGINS=${PAIRLY_CORS_ORIGINS:-$PAIRLY_PUBLIC_WEB_URL}"
    "PAIRLY_KEK_CURRENT=$PAIRLY_KEK_CURRENT"
    "PAIRLY_KEK_1=$PAIRLY_KEK_1"
    "PAIRLY_KEK_2=${PAIRLY_KEK_2:-}"
    "PAIRLY_BLIND_INDEX_KEY=$PAIRLY_BLIND_INDEX_KEY"
    "PAIRLY_SIGNED_LINK_SECRET=$PAIRLY_SIGNED_LINK_SECRET"
    "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY=$PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"
    "PAIRLY_AUDIT_ANCHOR_BUCKET=${PAIRLY_AUDIT_ANCHOR_BUCKET:-pairly-audit-anchors}"
    "PAIRLY_AUDIT_ANCHOR_LOCK_MODE=${PAIRLY_AUDIT_ANCHOR_LOCK_MODE:-GOVERNANCE}"
    "PAIRLY_AUDIT_ANCHOR_RETENTION_DAYS=${PAIRLY_AUDIT_ANCHOR_RETENTION_DAYS:-2555}"
    "PAIRLY_API_KEY_PEPPER=$PAIRLY_API_KEY_PEPPER"
    "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}"
    "OPENAI_API_KEY=${OPENAI_API_KEY:-}"
  )
  if [ -n "${PAIRLY_INTAKE_POLICIES:-}" ]; then
    LOCAL_ENV+=("PAIRLY_INTAKE_POLICIES=$PAIRLY_INTAKE_POLICIES")
  fi
  LOCAL_DOCUMENT_ORIGIN="$origin"
}

# The worker: the app env plus the audit signing key (only the worker holds it).
local_worker_env() {
  local_app_env
  LOCAL_ENV+=("PAIRLY_AUDIT_SIGNING_KEY=$PAIRLY_AUDIT_SIGNING_KEY")
}

# Alembic, pairly-admin and the development seed: the owner role (the migrate job).
local_owner_env() {
  local_base_env
  LOCAL_ENV+=(
    "PAIRLY_ENV=${PAIRLY_ENV:-development}"
    "PAIRLY_DATABASE_URL_OWNER=$(local_db_url pairly_owner "$PAIRLY_OWNER_DB_PASSWORD" "$POSTGRES_DB" postgresql+asyncpg)"
    "PAIRLY_PUBLIC_WEB_URL=$PAIRLY_PUBLIC_WEB_URL"
    "PAIRLY_INBOUND_EMAIL_DOMAIN=${PAIRLY_INBOUND_EMAIL_DOMAIN:-inbound.pairly.localhost}"
    "PAIRLY_KEK_CURRENT=$PAIRLY_KEK_CURRENT"
    "PAIRLY_KEK_1=$PAIRLY_KEK_1"
    "PAIRLY_KEK_2=${PAIRLY_KEK_2:-}"
    "PAIRLY_BLIND_INDEX_KEY=$PAIRLY_BLIND_INDEX_KEY"
  )
}

# The web app (the compose `web` service, with the outbox instead of Mailpit).
local_web_env() {
  local mode="$1"
  local_app_env
  local document_origin="$LOCAL_DOCUMENT_ORIGIN"
  local_base_env
  LOCAL_ENV+=(
    "NODE_ENV=$([ "$mode" = start ] && echo production || echo development)"
    "PAIRLY_ENV=${PAIRLY_ENV:-development}"
    "PORT=$LOCAL_WEB_PORT"
    "BETTER_AUTH_URL=$PAIRLY_PUBLIC_WEB_URL"
    "PAIRLY_AUTH_TRUSTED_ORIGINS=${PAIRLY_AUTH_TRUSTED_ORIGINS:-}"
    "BETTER_AUTH_SECRET=$BETTER_AUTH_SECRET"
    "PAIRLY_AUTH_DATABASE_URL=$(local_db_url pairly_auth "$PAIRLY_AUTH_DB_PASSWORD" "$POSTGRES_DB")"
    "PAIRLY_AUTH_MIGRATE_ON_START=true"
    "PAIRLY_AUTH_ISSUER=${PAIRLY_AUTH_ISSUER:-$PAIRLY_PUBLIC_WEB_URL}"
    "PAIRLY_AUTH_AUDIENCE=${PAIRLY_AUTH_AUDIENCE:-$PAIRLY_PUBLIC_WEB_URL}"
    "PAIRLY_API_INTERNAL_URL=http://127.0.0.1:${LOCAL_API_PORT}"
    # Signed document URLs come from the API's development route, not MinIO.
    "PAIRLY_DOCUMENT_ORIGIN=$document_origin"
    "PAIRLY_EMAIL_TRANSPORT=outbox"
    "PAIRLY_OUTBOX_DIR=$LOCAL_DIR/mail"
    "PAIRLY_SMTP_FROM=${PAIRLY_SMTP_FROM:-Pairly <no-reply@pairly.localhost>}"
    "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}"
    "GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}"
    "MICROSOFT_CLIENT_ID=${MICROSOFT_CLIENT_ID:-}"
    "MICROSOFT_CLIENT_SECRET=${MICROSOFT_CLIENT_SECRET:-}"
    "MICROSOFT_TENANT_ID=${MICROSOFT_TENANT_ID:-common}"
    "NEXT_TELEMETRY_DISABLED=1"
  )
}

local_require_app_secrets() {
  local_require PAIRLY_APP_DB_PASSWORD PAIRLY_OWNER_DB_PASSWORD PAIRLY_AUTH_DB_PASSWORD \
    REDIS_PASSWORD PAIRLY_S3_SECRET_KEY PAIRLY_KEK_CURRENT PAIRLY_KEK_1 \
    PAIRLY_BLIND_INDEX_KEY PAIRLY_SIGNED_LINK_SECRET PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY \
    PAIRLY_AUDIT_SIGNING_KEY PAIRLY_API_KEY_PEPPER BETTER_AUTH_SECRET
}

# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
#
# Hosted demo Postgres (Fly app pairly-demo-db, docs/hosted-demo.md): the same pinned
# pgvector image as the compose stack, with the same first-init script. On the first
# start of an empty volume the image runs infra/postgres/init/01-pairly-roles.sql as the
# superuser: it creates the roles pairly_owner (owns the schema, runs migrations),
# pairly_app (runtime, Row Level Security applies) and pairly_auth (Better Auth, owns
# schema auth only), and runs CREATE EXTENSION vector. The LangFuse script is left out
# (no observability profile in the demo).
#
# Build context is the repo root.
FROM pgvector/pgvector:0.8.7-pg16-bookworm
COPY --chown=root:root --chmod=0444 infra/postgres/init/01-pairly-roles.sql /docker-entrypoint-initdb.d/01-pairly-roles.sql

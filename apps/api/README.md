# pairly-api

Pairly REST API (`pairly_api`), built with FastAPI.

Run locally:

```sh
uv run alembic -c apps/api/alembic.ini upgrade head      # as the owner role
uv run uvicorn pairly_api.main:create_app --factory       # as the app role
```

Settings come from the environment and have no defaults for secrets. Required:
`PAIRLY_DATABASE_URL` (non-owner app role), `PAIRLY_AUTH_JWKS_URL`, `PAIRLY_AUTH_ISSUER`,
`PAIRLY_AUTH_AUDIENCE`. Migrations need `PAIRLY_DATABASE_URL_OWNER`. Optional:
`PAIRLY_ENV` (default `production`), `PAIRLY_CORS_ORIGINS` (comma separated exact origins),
`PAIRLY_DB_APP_ROLE` (default `pairly_app`), `OTEL_EXPORTER_OTLP_ENDPOINT`.

Endpoints: `/healthz` (liveness), `/readyz` (database check), `/v1/me`,
`/v1/audit/events` (filters, cursor pagination), `/v1/audit/export?format=csv|json`
(streamed), `/v1/audit/verify`, `/v1/openapi.json`. Every `/v1` call needs `Authorization: Bearer <Better Auth JWT>` and
`X-Pairly-Org: <organisation id>`. Errors use `application/problem+json`.

Database tests need `PAIRLY_TEST_DATABASE_URL` (app role) and
`PAIRLY_TEST_DATABASE_URL_OWNER` (owner role); without them they are skipped.

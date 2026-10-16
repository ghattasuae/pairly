# pairly-api

Pairly REST API (`pairly_api`), built with FastAPI.

Run locally:

```sh
uv run alembic -c apps/api/alembic.ini upgrade heads     # as the owner role
uv run uvicorn pairly_api.main:create_app --factory       # as the app role
```

Settings come from the environment and have no defaults for secrets. Required:
`PAIRLY_DATABASE_URL` (non-owner app role), `PAIRLY_AUTH_JWKS_URL`, `PAIRLY_AUTH_ISSUER`,
`PAIRLY_AUTH_AUDIENCE`. Migrations need `PAIRLY_DATABASE_URL_OWNER`. Optional:
`PAIRLY_ENV` (default `production`), `PAIRLY_CORS_ORIGINS` (comma separated exact origins),
`PAIRLY_DB_APP_ROLE` (default `pairly_app`), `OTEL_EXPORTER_OTLP_ENDPOINT`.

Endpoints: `/healthz` (liveness), `/readyz` (database check), `/v1/me`, `/v1/me/orgs`
(no org header), `/v1/members`, `/v1/invitations` (create, list, revoke, `preview`,
`accept`; accept needs no org header),
`/v1/audit/events` (filters, cursor pagination), `/v1/audit/export?format=csv|json`
(streamed), `/v1/audit/verify`, `/v1/openapi.json`. Every `/v1` call needs `Authorization: Bearer <Better Auth JWT>` and
`X-Pairly-Org: <organisation id>`. Errors use `application/problem+json`.

Database tests need `PAIRLY_TEST_DATABASE_URL` (app role) and
`PAIRLY_TEST_DATABASE_URL_OWNER` (owner role); without them they are skipped. The 0008
migration round trip also needs `PAIRLY_TEST_SCRATCH_DATABASE_URL_OWNER`, a second
database it can migrate down and up on its own (`scripts/ci_postgres.sh` creates both).

Operator CLI (owner role, `PAIRLY_DATABASE_URL_OWNER`): `uv run pairly-admin bootstrap-org
--name ... --slug ... --admin-email ...` creates an organisation and prints its admin
invite link (`make bootstrap-org` runs it in the migrate image).

Second factor: permissions whose action is `approve`, `decide`, `manage` or `export` need
a token whose `amr` contains `otp` (TOTP sign-in); otherwise 403 `second_factor_required`.

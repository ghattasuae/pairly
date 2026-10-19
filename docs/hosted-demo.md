# Hosted demo on Fly.io

A public, always-on copy of `make demo` that anyone can open in a browser: the seeded
organisation `demo`, with Hire, Pay and the expense example, in recorded model mode (no
model API key, no model spend). Everything in it is synthetic, and it resets every night.

Nothing here is deployed yet. This page is the plan and the exact steps; the files are in
`infra/fly/`. They were checked without a Fly account (TOML syntax, Dockerfile paths,
targets and build args, the build context, and every settings class against the demo
environment: `scripts/tests/test_fly_config.py`). The first real deploy is the first
end-to-end test, so the steps say what to check as you go.

## What runs where

Six Fly apps, one per service, in one region (`fra` in the files; any region works, set
`primary_region` in each file). One app per service rather than process groups of one
app, because Fly secrets belong to an app and reach every Machine of it: the services
must not share secrets (only the worker holds the audit signing key, only the ops app
holds the database owner's password).

| App | From | Size | Reachable at |
|---|---|---|---|
| `pairly-demo-web` | `infra/docker/web.Dockerfile` | shared-cpu-1x, 512 MB | `https://pairly-demo-web.fly.dev` |
| `pairly-demo-api` | `infra/docker/python.Dockerfile` (default target) | shared-cpu-1x, 512 MB | `https://pairly-demo-api.fly.dev` |
| `pairly-demo-worker` | `infra/docker/python.Dockerfile` (default target) | shared-cpu-1x, 1 GB | private, no service |
| `pairly-demo-db` | `infra/fly/db/Dockerfile` (pgvector image + init script) | shared-cpu-1x, 512 MB, 1 GB volume | private, `pairly-demo-db.internal:5432` |
| `pairly-demo-clamav` | `clamav/clamav-debian:1.5.4` | shared-cpu-2x, 4 GB, 1 GB volume | private, `pairly-demo-clamav.flycast:3310` |
| `pairly-demo-ops` | `infra/docker/python.Dockerfile` target `demo` | no Machine running; one scheduled Machine | private |

The database can instead be a Supabase project, which replaces `pairly-demo-db` (see
[Database on Supabase](#database-on-supabase) below). Both paths are kept.

Plus two Fly extensions: Upstash Redis (the job queue and the shared rate limits; arq
speaks the Redis protocol, as it does to Valkey locally) and Tigris (the S3-compatible
object store for documents and audit anchors).

Sizes follow the memory limits of `infra/docker-compose.yml` (web 384m, API 320m, worker
896m, Postgres 256m, ClamAV 2560m), rounded up to Fly's presets.

### Choices, and why

- **Postgres: our own Machine, not Fly Managed Postgres.** Managed Postgres supports
  pgvector, but its database users come in fixed role types (Schema Admin, Writer,
  Reader), and Fly does not grant CREATEROLE
  ([Fly community answer, 2026-09-24](https://community.fly.io/t/managed-postgres-can-the-admin-user-have-createrole-and-bypassrls-no-superuser/28697),
  [cluster configuration](https://docs.fly.io/mpg/cluster-configuration)). The three
  least-privilege roles of `infra/postgres/init/01-pairly-roles.sql` (an owner that runs
  migrations, an application role that Row Level Security applies to, and a Better Auth
  role that owns only schema `auth`) cannot be reproduced there. The legacy
  `fly postgres create` is deprecated ([docs](https://docs.fly.io/unmanaged-postgres)).
  So the demo runs the same pinned `pgvector/pgvector:0.8.7-pg16-bookworm` image as the
  compose stack on one Machine with a volume, and the image's first-start hook runs our
  init script unchanged. The trade-off: no managed backups or failover, which a demo that
  is rebuilt every night does not need. For a production deployment, revisit this.
  Supabase does allow the three roles (its `postgres` role has CREATEROLE), so it is the
  managed alternative: [Database on Supabase](#database-on-supabase).
- **ClamAV is part of the demo.** Outside development the worker accepts no fake
  scanner, and without clamd every upload waits for a person, so the demo's CVs would
  never be screened. It is the largest Machine (half the monthly cost below).
- **No mailbox at all.** Mailpit is not deployed, and no outbox page is exposed: a page
  that shows mail would show every message the demo sends, including other visitors'
  sign-in links. Instead, both the API and the web app use the `fake` email transport
  (sends nothing; refused in `production`), and visitors sign in through the demo sign-in
  page below. The demo cannot email anyone, so it cannot be used to send mail. Resend is
  wired in (`docs/email.md`) for a deployment that should send real mail; read the note
  there before turning it on for the public demo.
- **The seed is not a release command.** A Fly release command runs before the new
  version starts, in a temporary Machine with a 5 minute default timeout
  ([fly.toml reference](https://docs.fly.io/reference/configuration)). The demo seed
  needs the API, the worker and the web app running: it applies through the public
  careers API like a candidate and waits up to 7 minutes for the worker to screen the
  CVs. So the seed, and the migrations, run from the ops app: once by hand, then every
  night on a schedule (`scripts/hosted_demo.py`).
- **Environment `staging`.** The services run with every production check on (https
  URLs, TLS, no fake scanner, no file object store, no API docs page). Only the seed
  process, inside the reset job, runs as `development`, because `scripts/demo_seed.py`
  is a development seed and refuses anything else.

## Database on Supabase

Supabase replaces the `pairly-demo-db` Fly app for the **hosted deployment only**. Local
development, `make demo`, compose and CI stay on the native Postgres 16 + pgvector stack
(`infra/postgres/init/01-pairly-roles.sql`, `scripts/local_pg.sh`); nothing in them
changes or needs Supabase. The `db` app and its steps stay documented as the
alternative.

### Founder checklist

1. **Create the project** in the Supabase dashboard. Region: Supabase has no Middle East
   region. Mumbai (`ap-south-1`) is the closest to the UAE; Frankfurt (`eu-central-1`) is
   where the Fly apps run (`primary_region = "fra"`). Put the database next to the API
   and worker, since every request makes several database round trips: Frankfurt with
   the files as they are, or Mumbai if you also move every Fly app to a Mumbai region
   (check `fly platform regions`). Plan: Free to try it, Pro for a demo that must stay up
   (the Free plan pauses a project after a week without activity; see the costs below).
   Note the project ref and the session pooler host (Connect, Session pooler, for example
   `aws-0-eu-central-1.pooler.supabase.com`).
2. **Turn the Data API off**: open the Data API integration overview in the dashboard
   and switch **Enable Data API** off. Pairly never uses PostgREST or GraphQL.
3. **Enforce SSL**: Database Settings, SSL Configuration, "Enforce SSL on incoming
   connections" (it reboots the database).
4. **Secrets**, generated locally:

   ```bash
   uv run python scripts/gen_dev_env.py --fly ~/pairly-demo-secrets \
     --supabase <project ref> --supabase-pooler aws-0-eu-central-1.pooler.supabase.com
   ```

   It writes `api.env`, `worker.env`, `web.env`, `ops.env` and `supabase.env` (no
   `db.env`), with session pooler URLs and the matching settings.
5. **Run the bootstrap once**, as the project's `postgres` role, over the session pooler
   (port 5432) or the direct connection, never port 6543:

   ```bash
   set -a; . ~/pairly-demo-secrets/supabase.env; set +a
   psql "postgresql://postgres.<ref>:<postgres password>@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require" \
     -v ON_ERROR_STOP=1 -f infra/supabase/bootstrap.sql
   ```

   The last two result tables must show the three roles with every attribute `f` (except
   `rolinherit` for `pairly_owner`) and `f` in all three USAGE columns. Running it again is safe (it rotates the passwords to the ones in the
   file).
6. **Import the app secrets** (skip `pairly-demo-db` everywhere in the steps below:
   do not create the app, its volume, or deploy it):

   ```bash
   for a in api worker web ops; do
     fly secrets import --stage -a "pairly-demo-$a" < ~/pairly-demo-secrets/$a.env
   done
   rm -r ~/pairly-demo-secrets
   ```

7. **Migrate, deploy, seed**: steps 5 to 12 of [Steps for the founder](#steps-for-the-founder)
   unchanged (`hosted_demo.py migrate`, then the API, worker and web app, then `reset`).
   The web app creates the Better Auth tables in `pairly_auth` on its first start.
8. **Optional, recommended: verify the server certificate.** Download the CA from
   Database Settings, SSL Configuration, store it as a secret on api, worker, web and ops
   (`fly secrets set SUPABASE_CA_CERT="$(base64 < prod-ca-2021.crt)"`), uncomment the
   `[[files]]` block at the end of their `fly.toml`, then switch the URLs to
   `sslmode=verify-full` and set `PGSSLROOTCERT=/etc/pairly/supabase-ca.crt` (Python
   apps) or `NODE_EXTRA_CA_CERTS=/etc/pairly/supabase-ca.crt` (web). The generated URLs
   use `sslmode=require` (encrypted, server not verified) so the first deploy works
   before this step.

### How each role connects

| Role | Used by | Connection | Settings |
|---|---|---|---|
| `postgres` (Supabase's) | you, once, for `infra/supabase/bootstrap.sql` | session pooler or direct | |
| `pairly_owner` | migrations and the reset (`pairly-demo-ops` only) | session pooler `:5432` (or direct), user `pairly_owner.<ref>` | Alembic refuses a transaction pooler URL |
| `pairly_app` | API, worker (Row Level Security applies) | session pooler `:5432`, user `pairly_app.<ref>` | `PAIRLY_DATABASE_POOLER=session` |
| `pairly_auth` | web app (Better Auth) | session pooler `:5432`, user `pairly_auth.<ref>`, `&uselibpqcompat=true` | `PAIRLY_AUTH_DB_SCHEMA=pairly_auth` |

All URLs carry `sslmode=require` (or `verify-full`, step 8). Pairly translates libpq's
`sslmode=` to asyncpg's `ssl=` and back for psycopg (`pairly_core.config.to_asyncpg_url`,
`pairly_core.workflow.checkpoint.psycopg_conninfo`), so one URL serves both drivers.
node-postgres treats `sslmode=require` as `verify-full` unless `uselibpqcompat=true`
is set, hence that option on the web app's URL only.

Why the session pooler for everything: the pooler always has an IPv4 address, so it
works from any host, while the direct connection is IPv6 only unless the project buys
the IPv4 add-on (Pro and up). Session mode keeps one server connection per client connection, so asyncpg's
prepared statements, the LangGraph checkpointer's session setting, `statement_timeout`
on the Better Auth pool and the role-level `search_path` all behave as on a local
Postgres.

Transaction mode (port 6543) is supported for the application role but not needed for a
demo: `PAIRLY_DATABASE_POOLER=transaction` turns off asyncpg's statement cache
(`statement_cache_size=0`) and SQLAlchemy's (`prepared_statement_cache_size=0`, with
unique `prepared_statement_name_func` names), and then requires
`PAIRLY_CHECKPOINT_DATABASE_URL` on a session or direct connection, because the
checkpointer sets `app.org_id` for its whole session (`pairly_core.dbconn`). Pairly's own
tenant context is per transaction (`set_config(..., true)`), so it is safe under either
mode. arq and the rate limits use Upstash Redis and are unaffected.

### The `auth` schema collision

Supabase Auth owns schema `auth` in every project, and Pairly's Better Auth tables lived
in a schema of the same name. They must not share it (Supabase's tables would be next to
ours, and `pairly_auth` must own its schema). Resolution:

- The Better Auth schema is configurable, `PAIRLY_AUTH_DB_SCHEMA`, default `auth`, so
  local development and compose do not change. On Supabase it is `pairly_auth`.
- The bootstrap creates schema `pairly_auth` owned by role `pairly_auth` and sets that
  role's `search_path` to it (Better Auth's queries are unqualified, so the role's search
  path is what places them; a role setting also holds behind the pooler). It refuses
  `auth` and Supabase's other schemas as the name.
- Before migrating, the web app checks that the connection's current schema is
  `PAIRLY_AUTH_DB_SCHEMA` and is owned by the connected role
  (`apps/web/src/lib/auth/schema-check.ts`), so a wrong search path stops the migration
  instead of creating tables in Supabase's `auth` or in `public`.
- The nightly reset empties the Better Auth tables of `PAIRLY_AUTH_DB_SCHEMA`.

### What the bootstrap does, and the Data API

`infra/supabase/bootstrap.sql` is the Supabase counterpart of
`infra/postgres/init/01-pairly-roles.sql`:

- creates `pairly_owner`, `pairly_app` (NOINHERIT) and `pairly_auth` (NOINHERIT) with
  LOGIN and none of SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, and stops
  if an existing one has any of them or is a member of another role; same timeouts and
  search paths as locally;
- leaves the ownership of database `postgres` and schema `public` with Supabase (its own
  services depend on them) and gives `pairly_owner` CREATE on `public` and on the
  database (migration 0006 creates schema `lg_checkpoints`). Pairly's tables are still
  owned by `pairly_owner`, so forced Row Level Security and the owner-only lookup
  policies work exactly as locally;
- creates `vector` in `public` (Supabase's guide uses schema `extensions`; Pairly's
  migrations name the type without a schema) and stops if it is already installed
  elsewhere;
- removes every right of `PUBLIC`, `anon`, `authenticated` and `service_role` on
  `public` and on `pairly_auth`, and Supabase's default grants to those roles for objects
  the `postgres` role creates in `public`.

Pairly's data is kept out of Supabase's Data API in three independent ways: the Data API
is switched off (step 2: then "none of the auto-generated REST endpoints respond,
regardless of grants or RLS"); the Data API roles have no USAGE on the schemas; and no
table grants them anything, because Supabase's default privileges apply only to objects
created by its own roles while Pairly's are created by `pairly_owner`, and every
migration revokes table rights (and SECURITY DEFINER function rights) from `PUBLIC`.
Forced Row Level Security keyed on `app.org_id` applies on top.

Checked without a Supabase account: the bootstrap, the migrations (`upgrade heads` and
`check`), Better Auth's migration into `pairly_auth`, the checkpointer and the reset were
run against a local Postgres 16 that imitates Supabase's layout (a non-superuser
`postgres` role with CREATEROLE that owns the database, `anon`, `authenticated` and
`service_role` with Supabase's grants and default privileges, and a foreign-owned `auth`
schema). After them, no table, view or sequence in `public`, `lg_checkpoints` or
`pairly_auth` was reachable by the three Data API roles. The first real project is the
first end-to-end test.

### Supabase facts this relies on

Read 2026-10-19.

- Roles: "Superuser access is not given"; `postgres` gets "additional privileges ... to
  run some operations that are normally restricted to superusers"
  ([superuser access](https://supabase.com/docs/guides/database/postgres/roles-superuser)).
  Custom roles are created with `create role ... with login password`
  ([Postgres roles](https://supabase.com/docs/guides/database/postgres/roles)). "On
  Supabase the owner is `postgres`, which has `bypassrls`"
  ([Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security)).
  In Postgres, a role with CREATEROLE may create roles without SUPERUSER, REPLICATION or
  BYPASSRLS ([CREATE ROLE](https://www.postgresql.org/docs/17/sql-createrole.html)), which
  is all Pairly needs; a non-superuser may not even name SUPERUSER in `ALTER ROLE`, so
  the bootstrap checks the attributes instead of setting them again.
- Version: "Postgres 17 is what we currently default to on the Supabase platform"
  ([changelog](https://supabase.com/changelog/46080-self-hosted-supabase-upgrading-from-pg-15-to-17-breaking-change)).
  Pairly is tested on 16 and the bootstrap requires 16 or later. CI should add a
  Postgres 17 job (not done in this change).
- Connections: the direct connection is IPv6 unless the IPv4 add-on (Pro and up) is
  enabled; the Supavisor pooler, session mode on port 5432 and transaction mode on 6543,
  always has an IPv4 address; "Transaction mode does not support prepared statements";
  custom roles connect through the pooler as `[ROLE].[PROJECT-REF]`
  ([connecting](https://supabase.com/docs/guides/database/connecting-to-postgres),
  [IPv4 address](https://supabase.com/docs/guides/platform/ipv4-address)). asyncpg needs
  `statement_cache_size=0` behind it
  ([disabling prepared statements](https://supabase.com/docs/guides/troubleshooting/disabling-prepared-statements-qL8lEL)),
  and SQLAlchemy documents `prepared_statement_cache_size=0` and
  `prepared_statement_name_func` for PgBouncer-style poolers
  ([asyncpg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#prepared-statement-cache)).
  `statement_timeout` works through session mode, not transaction mode
  ([timeouts](https://supabase.com/docs/guides/database/postgres/timeouts)).
- SSL is not enforced by default; enforcement covers Postgres and Supavisor, and
  `verify-full` with the CA from the dashboard is the recommended mode
  ([SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement)).
- pgvector: `create extension vector with schema extensions` in Supabase's guide
  ([pgvector](https://supabase.com/docs/guides/database/extensions/pgvector)); Pairly
  uses `public`.
- Data API: "On existing projects, tables created in `public` receive `SELECT`, `INSERT`,
  `UPDATE`, and `DELETE` privileges for `anon`, `authenticated`, and `service_role` by
  default", and with the Data API disabled "none of the auto-generated REST endpoints
  respond, regardless of grants or RLS"
  ([securing the Data API](https://supabase.com/docs/guides/api/securing-your-api),
  [hardening](https://supabase.com/docs/guides/database/hardening-data-api)). New
  projects stopped exposing new tables automatically on 30 May 2026, and existing
  projects follow on 30 October 2026
  ([changelog](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically)).
  `PUBLIC` holds USAGE on `public` and EXECUTE on functions created there by default
  ([custom role privileges](https://supabase.com/docs/guides/troubleshooting/custom-role-inherits-privileges-that-were-not-explicitly-granted-ddaa1c)).
- Regions: Americas, Europe (including Frankfurt `eu-central-1`) and Asia-Pacific
  (including Mumbai `ap-south-1`); no Middle East region
  ([regions](https://supabase.com/docs/guides/platform/regions)).
- Pricing ([pricing](https://supabase.com/pricing)): see the cost table.

## Demo sign-in (`demo.public_signin`)

A visitor opens `https://pairly-demo-web.fly.dev/en/demo` (or `/ar/demo`; the sign-in
page links to it), picks a demo user (recruiter, hiring manager, interviewer, AP clerk,
invoice approver, finance controller, and the two expense example users), and is signed
in. The server creates a normal Better Auth magic link for that address (single use,
5 minutes) and redirects the browser to it instead of emailing it. No email is sent, no
mailbox is shown, and no other address can be reached.

Locks, all needed, and all off by default:

1. `PAIRLY_DEMO_PUBLIC_SIGNIN=true` on the web app and on the API, and `PAIRLY_ENV` is not
   `production` (both refuse it there, whatever the switch says).
2. The organisation `PAIRLY_DEMO_ORG_SLUG` (`demo`) has turned on its feature flag
   `demo.public_signin` (default off). The nightly reset turns it on, as the demo
   organisation's admin, and the change is audited like any flag change.
3. The address is one the API offers at that moment (`GET /v1/demo/signin-accounts`): a
   member of the demo organisation whose address is on a fixed list of the seeded demo
   users (`pairly_core.demo_signin.DEMO_ACCOUNTS`). Never the organisation admin, who
   manages members, API keys, webhooks and flags. The API finds the organisation through
   a SECURITY DEFINER function (migration `0034`) that returns its id only when the flag
   is on, then reads the members under Row Level Security.
4. The form post must come from the site itself (exact `Origin` match), and each client
   address gets 10 sign-ins per 10 minutes on the web server; the API endpoint allows 120
   requests a minute.

Because many visitors share each demo user, the web app closes the Better Auth endpoints
that would change the account for everybody else while the demo sign-in is on: turning
two-factor sign-in on or off, new backup codes, changing the email or name, deleting the
account, signing other sessions out, linking accounts. Actions that ask for a two-factor
step-up therefore cannot be completed on the hosted demo.

Code: `apps/web/src/lib/demo/`, `apps/web/src/app/api/demo/signin/route.ts`,
`apps/web/src/app/[locale]/(auth)/demo/page.tsx`, `apps/api/pairly_api/routes/demo.py`,
`packages/core/pairly_core/demo_signin.py`. Tests: `apps/web/src/lib/demo/demo-signin.test.ts`,
`apps/web/src/components/__tests__/demo-signin.test.tsx`, `apps/api/tests/test_demo_signin.py`.

## The nightly reset

`python scripts/hosted_demo.py reset` (in the ops image) gives every day a clean demo,
whatever visitors did the day before:

1. drops every table, view, sequence, function and type the owner role owns in schema
   `public` (pgvector's objects and the schemas stay, so no superuser is needed);
2. runs the migrations on the empty schema;
3. empties the Better Auth tables (users, sessions, tokens, rate limit rows), keeping the
   signing keys so the API's cached key set stays valid;
4. flushes the job queue;
5. deletes every object in the documents bucket (never the anchor bucket);
6. runs `scripts/demo_seed.py`;
7. turns on `demo.public_signin` for the demo organisation.

It refuses unless `PAIRLY_HOSTED_DEMO=1`, `PAIRLY_ENV=staging`, and
`PAIRLY_HOSTED_DEMO_DATABASE` equals the database named in the owner URL, so it cannot
wipe another database by mistake. While it runs (a few minutes), the demo shows errors.

## Rate limits for the demo

The demo deployment serves only the demo organisation, so its limits are set for the
whole deployment in the `[env]` of the API, worker and ops files: API keys 60 requests a
minute and 7 days at most, batch intake 20 documents a minute, uploads 5 MB. The public
careers limits are fixed in code (10 applications an hour per address and organisation).
Behind Fly's proxy the API sees the proxy's address, not the visitor's, so per-address
limits on the API act as one limit for all visitors together; that is stricter, not
looser. Fly's own `concurrency` limits cap requests per Machine (`hard_limit` 40 on the
API, 50 on the web app).

## Secrets

Generated locally, one file per app, each holding only what that app needs:

```bash
uv run python scripts/gen_dev_env.py --fly ~/pairly-demo-secrets
```

It writes `db.env`, `api.env`, `worker.env`, `web.env` and `ops.env` (mode 0600, never
overwritten) and prints the file and key names, never a value. The database passwords
are written into the connection URLs of the apps that use them, so the files match.

| App | Secrets |
|---|---|
| db | `POSTGRES_PASSWORD`, `PAIRLY_OWNER_DB_PASSWORD`, `PAIRLY_APP_DB_PASSWORD`, `PAIRLY_AUTH_DB_PASSWORD` |
| api | `PAIRLY_DATABASE_URL` (app role), `PAIRLY_KEK_1`, `PAIRLY_BLIND_INDEX_KEY`, `PAIRLY_API_KEY_PEPPER`, `PAIRLY_SIGNED_LINK_SECRET`, `PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY` |
| worker | the API's, plus `PAIRLY_AUDIT_SIGNING_KEY` |
| web | `BETTER_AUTH_SECRET`, `PAIRLY_AUTH_DATABASE_URL` (auth role) |
| ops | the API's, plus `PAIRLY_DATABASE_URL_OWNER` and `PAIRLY_AUTH_DATABASE_URL` |
| api, worker, ops, by hand | `PAIRLY_REDIS_URL` (from Upstash), `PAIRLY_S3_ACCESS_KEY`, `PAIRLY_S3_SECRET_KEY` (from Tigris) |

`PAIRLY_HIRE_TURNSTILE_SECRET` is not a secret here: the web app has no Turnstile widget
yet and always sends Cloudflare's documented test token, so the API checks it with
Cloudflare's documented always-pass test secret
([Cloudflare testing docs](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)),
set in the `[env]` of the files. The demo careers form therefore has no real bot
protection; its rate limits and the nightly reset still apply. A real Turnstile site
would reject the seed's test token, so it needs the widget first.

## Monthly cost estimate

From Fly's published prices ([docs.fly.io/about/pricing](https://docs.fly.io/about/pricing),
read 2026-10-17; the Machine prices on that page are for the `iad` region, and other
regions may differ), the Upstash page ([docs.fly.io/upstash/redis](https://docs.fly.io/upstash/redis),
read 2026-10-17) and Tigris's page ([tigrisdata.com/pricing](https://www.tigrisdata.com/pricing/),
read 2026-10-17). One Machine per app (`--ha=false`), every Machine always on.

| Item | Price | Monthly |
|---|---|---|
| web, shared-cpu-1x 512 MB | $3.69/month | $3.69 |
| api, shared-cpu-1x 512 MB | $3.69/month | $3.69 |
| worker, shared-cpu-1x 1 GB | $6.70/month | $6.70 |
| db, shared-cpu-1x 512 MB | $3.69/month | $3.69 |
| clamav, shared-cpu-2x 4 GB | $26.78/month | $26.78 |
| Volumes, 1 GB each for db and clamav | $0.15/GB/month | $0.30 |
| Upstash Redis, fixed 250 MB plan | $10/month | $10.00 |
| Tigris, under the free allowance (5 GB storage, 10,000 class A and 100,000 class B requests a month) | $0.02/GB/month beyond it, no egress charge | $0.00 |
| Shared IPv4 and IPv6, first 10 certificates | free | $0.00 |
| **Fixed total** | | **$54.85** |

Variable, small for a demo:

- ops: one Machine runs the reset once a day, billed by the second while it runs. If a
  reset takes 20 minutes at the 1 GB price, that is about 10 hours a month, roughly
  $0.09 (an estimate from the $6.70/month price, not a published figure). Its stopped
  Machine's root file system is billed at $0.15/GB/month.
- Outbound data: $0.02/GB in North America and Europe, $0.04/GB in Asia Pacific,
  Oceania and South America, $0.12/GB in Africa and India.
- Upstash pay-as-you-go ($0.20 per 100,000 requests) is not cheaper here: arq polls the
  queue every 0.5 seconds by default, which alone is over 5 million polls a month.

With the database on Supabase instead of the `db` app (prices from
[supabase.com/pricing](https://supabase.com/pricing), read 2026-10-19):

| Item | Free | Pro |
|---|---|---|
| Price | $0 | from $25/month, including $10/month of compute credits that cover the Micro instance |
| Database | 500 MB, shared CPU, 500 MB RAM | 8 GB disk included, then $0.125/GB |
| Egress | 5 GB | 250 GB, then $0.09/GB |
| Backups | none | daily, kept 7 days |
| Paused | after 1 week of inactivity | never |
| Limit | 2 active projects | |
| Fly total (the table above without the db Machine and its volume, $3.84) | **$51.01** | **$76.01** |

The synthetic demo fits in 500 MB, but the Free plan's pause matters for a recruiter
demo: a project nobody used for a week is paused and the demo is down until someone
restores it in the dashboard. Do not count on the nightly reset to keep it awake (the
pricing page does not define activity). Pro is the dependable choice for a link on a CV;
Free is fine to try the setup.

Ways to spend less: ClamAV on shared-cpu-1x with 2 GB ($11.39/month) plus extra memory
(about $6/GB/month on the same page) instead of 4 GB; `auto_stop_machines = "stop"` on
the web app and the API (they then start on the first request, slowly).

## Steps for the founder

Run everything from the repository root. `flyctl` builds the images remotely, so no
local Docker is needed.

1. **Account and CLI.** Install flyctl (`brew install flyctl`), then `fly auth signup`
   (or `fly auth login`). Add a payment method in the dashboard.
2. **Apps.** Create the six apps (names are global; if one is taken, pick another prefix
   and change it in every `infra/fly/*/fly.toml`, including the URLs in `[env]`, and pass
   `--fly-prefix` in step 3):

   ```bash
   for a in db clamav api worker web ops; do fly apps create "pairly-demo-$a"; done
   ```

3. **Secrets.** Generate and import them, then delete the folder:

   ```bash
   uv run python scripts/gen_dev_env.py --fly ~/pairly-demo-secrets
   for a in db api worker web ops; do
     fly secrets import --stage -a "pairly-demo-$a" < ~/pairly-demo-secrets/$a.env
   done
   rm -r ~/pairly-demo-secrets
   ```

4. **Postgres.** (With the database on Supabase, skip this step: follow
   [Database on Supabase](#database-on-supabase) instead.) Create the volume and deploy. On the first start the image runs the
   init script as the superuser: it creates `pairly_owner`, `pairly_app` and
   `pairly_auth`, schema `auth`, and runs `CREATE EXTENSION IF NOT EXISTS vector WITH
   SCHEMA public`.

   ```bash
   fly volumes create pg_data --size 1 --region fra -a pairly-demo-db --yes
   fly deploy . --config infra/fly/db/fly.toml --dockerfile infra/fly/db/Dockerfile --ha=false
   ```

   Check it: `fly ssh console -a pairly-demo-db`, then
   `PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U postgres -d pairly -c '\dx' -c '\du'`
   lists `vector` and the three roles. If `vector` is missing (the volume was initialised
   some other way), run `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;` there
   as `postgres`: migrations never need superuser rights after that.
5. **Redis (Upstash).** `fly redis create` (name `pairly-demo-redis`, region `fra`, no
   replicas, eviction off), then `fly redis update pairly-demo-redis` and choose the
   250 MB plan. Set the connection URL it prints (a `redis://` URL to a private
   `fly-<name>.upstash.io` address) on the three Python apps. Start the line with a space so the shell does not keep it in
   its history:

   ```bash
    for a in api worker ops; do fly secrets set --stage -a "pairly-demo-$a" PAIRLY_REDIS_URL='<url>'; done
   ```

6. **Object store (Tigris).** Create the two buckets:
   `fly storage create -n pairly-demo-documents` and
   `fly storage create -n pairly-demo-anchors` (private, the default). Pairly uses one
   access key for both: in the Tigris dashboard (`fly storage dashboard pairly-demo-documents`)
   create an access key with read and write on both buckets, and set it on the Python apps:

   ```bash
    for a in api worker ops; do fly secrets set --stage -a "pairly-demo-$a" \
      PAIRLY_S3_ACCESS_KEY='<id>' PAIRLY_S3_SECRET_KEY='<secret>'; done
   ```

   If `fly storage create` attached its own `AWS_*` and `BUCKET_NAME` secrets to an app,
   remove them (`fly secrets unset`); Pairly does not read them. If the document viewer
   cannot load a PDF, add a CORS rule on `pairly-demo-documents` allowing `GET` from
   `https://pairly-demo-web.fly.dev` (not verified without an account).

   **Write-once gap.** Tigris's S3 compatibility list documents no S3 Object Lock
   ([Tigris S3 API](https://www.tigrisdata.com/docs/api/s3/)), so the demo sets
   `PAIRLY_AUDIT_ANCHOR_LOCK_MODE=none`. Audit anchors then keep two of their three
   write-once layers (`pairly_core.audit_anchor.store`): every write is a conditional put
   that never overwrites, and Pairly has no delete. The third, a storage-level retention
   lock, is missing: someone holding the Tigris credentials could delete anchors. That is
   acceptable for a synthetic demo and not for production, which needs a store with
   Object Lock (`COMPLIANCE` mode).
7. **ClamAV.** It needs a private address only:

   ```bash
   fly volumes create clamav_db --size 1 --region fra -a pairly-demo-clamav --yes
   fly deploy --config infra/fly/clamav/fly.toml --ha=false --no-public-ips --flycast
   ```

   The first start downloads the signatures (several minutes).
8. **Ops image and migrations.** Build and push the ops image, then migrate:

   ```bash
   fly deploy . --config infra/fly/ops/fly.toml --dockerfile infra/docker/python.Dockerfile \
     --build-target demo --build-only --push --image-label demo
   fly machine run -a pairly-demo-ops --region fra --vm-memory 1024 --rm \
     registry.fly.io/pairly-demo-ops:demo python scripts/hosted_demo.py migrate
   ```

9. **API, worker, web.**

   ```bash
   fly deploy . --config infra/fly/api/fly.toml --dockerfile infra/docker/python.Dockerfile --ha=false
   fly deploy . --config infra/fly/worker/fly.toml --dockerfile infra/docker/python.Dockerfile --ha=false
   fly deploy . --config infra/fly/web/fly.toml --dockerfile infra/docker/web.Dockerfile --ha=false
   ```

   Check: `https://pairly-demo-api.fly.dev/readyz` and
   `https://pairly-demo-web.fly.dev/api/healthz` answer `ok`. The web app creates its
   Better Auth tables on its first start.
10. **Seed.** The first reset seeds the demo (a few minutes; it waits for the worker):

    ```bash
    fly machine run -a pairly-demo-ops --region fra --vm-memory 1024 --rm \
      registry.fly.io/pairly-demo-ops:demo python scripts/hosted_demo.py reset
    ```

11. **Open it.** `https://pairly-demo-web.fly.dev/en/demo`: sign in as the recruiter, open
    Hire and the two backend engineer jobs. The careers page is
    `https://pairly-demo-web.fly.dev/en/careers/demo`.
12. **Nightly reset.** Schedule the same command daily (Fly runs it once a day; the exact
    hour is Fly's choice, [fly machine run](https://docs.fly.io/flyctl/cmd/fly_machine_run)):

    ```bash
    fly machine run -a pairly-demo-ops --region fra --vm-memory 1024 --schedule daily \
      registry.fly.io/pairly-demo-ops:demo python scripts/hosted_demo.py reset
    ```

    To reset now: run step 10 again. To see the scheduled Machine and its logs:
    `fly machine list -a pairly-demo-ops`, `fly logs -a pairly-demo-ops`.

### Updating the demo

After a change on `main`: rebuild the ops image (step 8, first command), run `migrate`
(step 8, second command), deploy the API, worker and web app (step 9), then reset
(step 10). The scheduled Machine keeps the image it was created with: update it with
`fly machine update <id> --image registry.fly.io/pairly-demo-ops:demo -a pairly-demo-ops`.

### Taking it down

`fly apps destroy` each app removes its Machines, volumes and secrets:

```bash
for a in web api worker ops clamav db; do fly apps destroy "pairly-demo-$a" --yes; done
```

Then delete the Redis database (`fly redis destroy pairly-demo-redis`) and the two Tigris
buckets (`fly storage destroy pairly-demo-documents`, `fly storage destroy pairly-demo-anchors`).
Billing stops with them.

## Known limits of the hosted demo

- No email of any kind: candidates, approvers and vendors are never emailed, so the
  flows that start from an emailed link (candidate portal, self-scheduling, approval
  links, vendor onboarding) can be shown only from the staff side.
- No inbound email: the three Pay demo emails are written by the seed but not delivered
  (`make inbound-drop` is development only). The seeded invoices are there. Resend's
  inbound webhook exists for a deployment that wants it (`docs/email.md`).
- Shared accounts: what one visitor does, the next one sees until the nightly reset.
- Two-factor step-up actions cannot be completed (see the demo sign-in section).
- The careers form has no real bot protection (see Secrets).
- Not yet verified on Fly itself: the build context resolution of `fly deploy .` with
  `--config` in a subfolder, Upstash's support of the commands arq and the shared rate
  limiter use (Lua scripts), Tigris CORS for the document viewer, and the reset's run
  time. Check them on the first deploy.

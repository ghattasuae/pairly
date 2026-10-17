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
- **ClamAV is part of the demo.** Outside development the worker accepts no fake
  scanner, and without clamd every upload waits for a person, so the demo's CVs would
  never be screened. It is the largest Machine (half the monthly cost below).
- **No mailbox at all.** Mailpit is not deployed, and no outbox page is exposed: a page
  that shows mail would show every message the demo sends, including other visitors'
  sign-in links. Instead, both the API and the web app use the `fake` email transport
  (sends nothing; refused in `production`), and visitors sign in through the demo sign-in
  page below. The demo cannot email anyone, so it cannot be used to send mail.
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

4. **Postgres.** Create the volume and deploy. On the first start the image runs the
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
  (`make inbound-drop` is development only). The seeded invoices are there.
- Shared accounts: what one visitor does, the next one sees until the nightly reset.
- Two-factor step-up actions cannot be completed (see the demo sign-in section).
- The careers form has no real bot protection (see Secrets).
- Not yet verified on Fly itself: the build context resolution of `fly deploy .` with
  `--config` in a subfolder, Upstash's support of the commands arq and the shared rate
  limiter use (Lua scripts), Tigris CORS for the document viewer, and the reset's run
  time. Check them on the first deploy.

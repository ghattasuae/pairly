# @pairly/web

The Pairly web app: Next.js (App Router, TypeScript strict), Tailwind CSS with shadcn/ui
style components on our own design tokens, Better Auth, next-intl (English and Arabic with
RTL), light and dark themes, Storybook.

The web app never decides access. It signs people in, gets a short-lived token from Better
Auth, and calls the FastAPI API with it. The API checks every permission. The sidebar reads
permissions from `/v1/me` only to hide links nobody could use.

## Run it

```bash
make dev    # native stack: Postgres, Valkey, API, worker and this app on http://localhost:3100
```

`make dev` runs `next dev` on 127.0.0.1:3100 in the background (log in
`.local/logs/web.log`) with the variables it needs, and the app applies the Better Auth
migrations itself on start (`PAIRLY_AUTH_MIGRATE_ON_START=true`). `LOCAL_WEB_MODE=start
make dev` builds once and runs `next start` instead. Stop with `make local-down`.

Create an organisation with `make bootstrap-org` (see below) and open the printed invite
link. Email is not sent in the native stack: magic links and invitations are written to
`.local/mail` (`PAIRLY_EMAIL_TRANSPORT=outbox`, see `src/lib/outbox.ts`). The newest
message is the last line of `.local/mail/index.jsonl`; its `<id>.json` holds the text and
HTML, its `<id>.eml` the raw message. The outbox is refused unless `PAIRLY_ENV` is
`development` or `test`.

Documents open through short-lived signed URLs from the API's development route
(`PAIRLY_DOCUMENT_ORIGIN` is the API origin, `http://localhost:8000`).

The Docker Compose stack still works for CI and self-hosters: `PROFILES=web make
docker-dev`. There email goes to Mailpit (http://localhost:8025) and documents to MinIO.

Other scripts:

| Command | What it does |
|---|---|
| `pnpm --filter @pairly/web build` | Production build (`output: "standalone"`) |
| `pnpm --filter @pairly/web typecheck` | `next typegen` then `tsc` |
| `pnpm --filter @pairly/web storybook` | Storybook on port 6006 |
| `pnpm test:storybook` (repo root) | Build Storybook, then render every story in headless Chromium; fails on any error |
| `pnpm --filter @pairly/web auth:schema` | Print pending Better Auth SQL without applying it |
| `pnpm test` (repo root) | Vitest, web tests run in jsdom as the `web` project |
| `pnpm gen:api` (repo root) | Regenerate API types in `packages/shared-ts` |

## Layout

```
src/
  proxy.ts               CSP nonce, sign-in gate (fast path), locale routing
  instrumentation.ts     Better Auth migrations on start (when enabled)
  app/[locale]/(auth)    sign-in, check-email, sign-in/confirm, two-factor, invite,
                         approvals/link (signed approval links)
  app/[locale]/(app)     signed-in shell: home, inbox (+ inbox/[id]), hire, pay, audit,
                         decisions/[id], documents/[id], settings (notifications,
                         security, members, policies, flags, api-keys, webhooks)
  app/api/auth/[...all]  Better Auth (magic link, 2FA, organizations, /token, /jwks)
  app/api/notifications/stream  the notification event stream, passed through (SSE)
  app/api/decisions/[id]/receipt  receipt PDF, passed through
  app/api/audit/export   audit export (CSV, JSON), passed through
  app/api/healthz        container liveness
  components/ui          primitives (button, input, form controls, dropdown, dialog,
                         confirm dialog, toaster, tooltip, kbd)
  components/shell       app shell, sidebar, org/locale/theme switchers, command palette
  components/states      action needed, not provisioned, unavailable, empty state, step-up
  components/auth        sign-in form, magic link confirm, 2FA verify and setup
  components/inbox       unified approval inbox (split view, shortcuts, decisions)
  components/document    document viewer (PDF.js, highlights)
  components/decisions   decision receipt, replay, appeals
  components/audit       audit viewer
  components/settings    settings pages
  components/notifications  live notifications and the bell
  lib/auth               Better Auth config, 2FA gate, server helpers, browser client
  lib/api/server.ts      server-side API client (JWT + X-Pairly-Org)
  lib/api/call.ts        one call, one small result (`code` from problem+json)
  lib/api/bff.ts         helpers for the pass-through route handlers
  lib/provisioning.ts    /v1/me to viewer state
  lib/actions            server actions: invitations, organisation switch, approvals and
                         approval links, decisions, audit, notifications, settings, search
  lib/org-mirror.ts      Better Auth organization mirror (same UUID as the API)
  lib/nav.ts             which sections the sidebar shows
messages/en.json, ar.json  every UI string
```

## Auth

- **Magic link** through SMTP (Mailpit in the Docker stack; the file outbox in the native stack). The email links to
  `/<locale>/sign-in/confirm#token=...`. The token sits in the URL fragment, so it never
  reaches server logs, and it is only spent when the person presses "Sign in", so mail
  scanners that open links cannot use it up. Tokens are stored hashed and expire after
  5 minutes.
- **TOTP 2FA** (two-factor plugin, with backup codes). Better Auth only enforces it for
  password sign-ins, so `lib/auth/two-factor-gate.ts` adds the same check after magic link
  and SSO sign-ins: the new session is deleted and the user must enter a code first.
  Users turn it on under Account, Security.
- **Google and Microsoft SSO** are configured but stay off until both the client id and
  secret are set.
- **Organizations plugin** only mirrors the API organisation active in the session (see
  below). Users cannot create organizations from the web app.
- **JWT plugin**: EdDSA (Ed25519) tokens, 5 minute lifetime, keys at `/api/auth/jwks`.
  Issuer and audience come from `PAIRLY_AUTH_ISSUER` and `PAIRLY_AUTH_AUDIENCE`, the same
  variables the API checks. Tokens hold `sub`, `iat`, `exp`, `iss`, `aud`, `amr`, `email` and `email_verified`. IDs are
  UUIDs, so an organization id goes to the API unchanged as `X-Pairly-Org`.
- **Database**: Postgres as role `pairly_auth`, whose search path is schema `auth`. It has
  no rights on the Pairly tables.
- **Protections**: secure, http-only, SameSite=Lax cookies (prefix `pairly`), Better Auth's
  origin checks on state-changing requests, rate limits stored in the database (magic link:
  5 per 5 minutes per IP plus 3 per 15 minutes per address; 2FA: 5 per minute), a nonce-based Content Security Policy, and
  standard security headers.

Settings are read when the server starts handling requests, never at build time. A missing
secret stops the server. In development, values the compose stack composes for the
container (`PAIRLY_AUTH_DATABASE_URL` and friends) are derived from the repo-root `.env`.

## Identity and provisioning (WP0.8)

Staff are invite-only. Nobody can sign up: magic links and SSO never create a Better Auth
user, and access comes only from accepting an invitation in the API.

- **First organisation**: `make bootstrap-org NAME="Acme Ltd" SLUG=acme ADMIN_EMAIL=you@acme.test`
  (or `uv run pairly-admin bootstrap-org ...` with `PAIRLY_DATABASE_URL_OWNER`). It creates
  the API organisation and an admin invitation, and prints the accept link once.
- **Invitations**: the API owns them (`/v1/invitations`). An admin invites from
  Settings, Members. This server emails the link (`lib/email.ts`) right after the API
  returns the token, so the raw token is never stored or queued; if SMTP fails, the
  inviter sees the link to share by hand (only the invited address can accept it).
- **Accepting**: `/[locale]/invite#token=...`. The token is in the fragment, so it never
  reaches a server log; the page keeps it in localStorage for an hour so the invitee can
  come back after the magic link. Opening it changes nothing. A signed-out
  invitee asks for a magic link to the invited address (`prepareInviteSignIn` creates the
  Better Auth user for that address only, after the API confirms the token), comes back
  through 2FA if enabled (`next` survives both steps), and presses "Accept and join". The
  API checks the token and the JWT's verified email, then creates the user and membership.
- **Organisation mirror**: the API is the source of truth. `GET /v1/me/orgs` drives the
  org switcher. `lib/org-mirror.ts` creates the Better Auth organization with the SAME
  UUID and a plain member row, in-process through Better Auth's adapter, only for an
  organisation the API just confirmed. Better Auth's `/organization/*` HTTP endpoints are
  blocked. The CLI never writes to the `auth` schema.
- **Second factor (brief 6.1)**: each session records how it was signed in (`amr`:
  `otp` after a TOTP code, `otp` plus `backup` after a backup code, else `email`, `sso`
  or `none`, see `lib/auth/amr.ts`),
  and the JWT carries it. The API refuses any `approve`, `decide`, `manage` or `export`
  permission without `otp`. Pages show "Set up two-factor to continue" (or "sign in
  again") to people who hold such permissions.
- **Token minting**: `/api/auth/token` is closed to HTTP (route handler plus
  `disabledPaths`). Only this server mints tokens, in-process. `/api/auth/jwks` is public.
- **Magic link limits**: 5 per 5 minutes per IP (Better Auth) and 3 per 15 minutes per
  address (`lib/auth/magic-link-limit.ts`, which also prunes expired rows in the same
  statement). Over the limit, or for an unknown address, the response is the same and no
  email is sent.
- **Backup codes** count as a second factor: the session's `amr` claim is
  `["otp", "backup"]`. The sign-in is logged as a structured warning
  (`auth.backup_code_sign_in`, user id only) and Home tells the user how many codes remain.

## End-to-end and accessibility suite

`e2e/` holds a Playwright suite (Chromium, one worker, headless) with axe accessibility
checks, copy checks (raw message keys, em dashes) and RTL checks across en/ar and
light/dark. It runs against the running native stack, not a server Playwright starts,
because the flows need the API, the worker and Postgres too. It is not part of CI yet.

1. The suite opens `http://localhost:3100` (`http://localhost:<PAIRLY_WEB_PORT>`), the
   host of `PAIRLY_PUBLIC_WEB_URL`, so magic links and invitations come back to the
   origin whose cookies and storage the browser holds. No extra trusted origin is
   needed. To use another address (for example `http://127.0.0.1:3100`), set
   `PAIRLY_E2E_BASE_URL` and add that origin to `PAIRLY_AUTH_TRUSTED_ORIGINS` in the
   repo-root `.env`, or sign-in requests fail the origin check.
2. Start the native stack: `make dev` (or `LOCAL_WEB_MODE=start make dev` for a
   production build, which answers faster than `next dev` compiling each page on first
   use). Port 3100 on localhost must be this stack's web app; another local server on
   that port will answer instead. `make local-status` shows the health of each part.
3. Install the browser once: `pnpm --filter @pairly/web exec playwright install chromium`.
4. Run it:

   ```bash
   pnpm --filter @pairly/web e2e                                   # everything
   pnpm --filter @pairly/web e2e e2e/tests/04-security.spec.ts     # one spec
   ```

   `PAIRLY_E2E_BASE_URL` points the suite at another address (it must be trusted, see
   step 1); `PAIRLY_WEB_PORT` changes the default port. `PAIRLY_E2E_SCRATCH` sets where
   screenshots, saved sessions and the findings (`findings/axe.ndjson`, `copy.ndjson`,
   `rtl.ndjson`) are written. Axe results are
   logged there rather than failing the run, so read `axe.ndjson` after each run.
   `09-m1-acceptance` writes its screenshots to `PAIRLY_E2E_M1_DIR`.

The suite reads email from the outbox (`e2e/utils/mail.ts`, `.local/mail`, or
`PAIRLY_OUTBOX_DIR`). The `make` calls it makes (`bootstrap-org`, `demo-expense`,
`dev-seed-approval`, `audit-anchor-now`) run against the native stack.

Order matters: `01-full-flow` bootstraps an organisation (`make bootstrap-org`), accepts
the invite, turns on TOTP and saves sessions (and `run-state.json`) that the later specs
reuse. `06-inbox`, `07-approval-link` and `08-audit-decisions-settings` seed approvals
with `make dev-seed-approval` (development only, see below). `09-m1-acceptance` stands
alone (its own organisation, seeded with `make demo-expense`, recorded model mode). Run
`make local-down` when you are done.

If you point the suite at another host than the one in emailed links, set
`PAIRLY_E2E_EMAILED_HOST` to the emailed host so links are rewritten to yours.

Against the Docker Compose stack (`PROFILES="web scan" make docker-dev`) instead, set
`PAIRLY_E2E_STACK=docker` (the `make` calls then run with `STACK=docker`) and
`PAIRLY_E2E_MAIL=mailpit` (read email from Mailpit at `MAILPIT_URL`, default
`http://127.0.0.1:8025`). `PAIRLY_E2E_COMPOSE_FILE` adds compose files for those `make`
calls (for example `infra/docker-compose.yml -f my-override.yml`) and implies
`PAIRLY_E2E_STACK=docker`.

Development seed: `make dev-seed-approval ORG=<slug> EMAIL=<member email> ARGS="..."` gives
the member the `approver` role in Pay, starts a placeholder run and requests one
`pay.invoice` approval through the real approval service (`--overdue`, `--link approve`
for a signed link token, `--decision` for a synthetic decision record). It prints JSON and
refuses to run without `PAIRLY_DEVSEED=1` (the target sets it) or with
`PAIRLY_ENV=production`. After a decision the API logs that the placeholder run cannot
resume: expected.

## Inbox, documents, decisions, audit and settings (WP1.13)

- **Inbox** (`/inbox`): every approval the person may decide, across workflows. List on
  the start side, the open item on the end side; filters, search and the open item live
  in the URL. Keys: J and K move, Enter opens, A approves, R rejects, C asks for changes,
  / searches, ? shows help. Approve and reject are confirmed in a dialog (final and
  external); reject and request changes need a reason. Decisions are optimistic and roll
  back on error; each attempt has an `Idempotency-Key` (a retry of the same decision reuses
  it). `second_factor_required` shows the step-up prompt: sign in again with the code and
  come back to `/inbox/<id>`. The next item is prefetched. New `approvals` and `sla`
  notifications on the live stream refresh the list.
- **Approval links** (`/approvals/link#<token>`, as the API builds them): the token is read
  from the fragment and removed from the address bar, kept in this browser for at most 30
  minutes, described (no change), and decided only on the confirm button by the bound
  user signed in with a code. Used, expired, invalid and other people's links look the
  same.
- **Expenses** (`/expense`, the M1 example workflow): submit a receipt with a note and
  follow your notes (reviewers see all). The receipt goes through its own route handler,
  `POST /api/expense/notes` (same guards and 26 MB limit as policy uploads, plus one
  `Idempotency-Key` per submission), to `POST /v1/expense/notes`.
- **Approval context convention** (workflow-agnostic): the inbox draws evidence from an
  approval's `context` when it has `document_id`, `evidence[]` (`label` or `field`,
  `quote`, `page`, `bboxes` or `bbox`), `guardrail_findings[]` (`type`, `code`,
  `severity`, `page`, `bbox`, `evidence`, optional `remove`: the text was left out of
  what the model read) and `decision_id` or `decision_ids[]`. Other keys show as plain
  data. An item with guardrail findings opens with a warning above the decision buttons:
  one sentence per finding from the explanation keys, the flagged text quoted (the
  context, like the inbox, is for reviewers only), whether extraction ignored it, and a
  button that shows it on the page. The list row says "Needs a person: guardrail flag"
  (the API's `guardrail_findings` count on each inbox item). "Open the document" passes
  `?approval=<id>`, so the document page draws the same findings when the approval names
  that document.
- **Document viewer**: PDF.js (pdfjs-dist, Apache-2.0) renders pages; its worker and
  standard fonts are copied to `public/pdfjs/` by `scripts/copy-pdfjs.mjs` before `dev`,
  `build` and Storybook, and load from our own origin (`worker-src 'self'`). The bytes come
  through the API's short-lived signed URL, so the CSP also allows that one origin
  (`PAIRLY_DOCUMENT_ORIGIN`, exact origin only; in development the compose MinIO port).
  Evidence is a solid teal box, a guardrail finding a dashed, striped red box, with a
  legend and one sentence per finding from the API's explanation keys (en and ar).
- **Decision viewer** (`/decisions/<id>`): the receipt as a page, PDF in English or Arabic,
  replay with the difference, appeals (file, resolve with a written outcome and step-up).
- **Audit viewer** (`/audit`): exact-match filters in the URL, cursor pages, a search of
  the loaded page, event details with hashes, chain check (queued for long chains) and
  anchors, CSV or JSON export (step-up).
- **Settings**: notification preferences (the unsubscribe page links here), feature flags,
  API keys (secret shown once), webhooks (URLs as the API redacts them, delivery log),
  policies (documents, guardrail warnings, upload, two-admin override, rule sets and dry
  run). `Cmd/Ctrl+K` also searches (`/v1/search`); the bell shows unread notifications
  live.

The browser never talks to the API: pages and server actions call it with the
server-minted token, and three route handlers pass through what must stream (the event
stream, receipt PDFs and audit exports). They refuse cross-site requests
(`Sec-Fetch-Site`), and the export also needs a custom header a link cannot send.

Server actions keep Next's default 1 MB body limit. Policy documents (up to 25 MB) are
uploaded through their own route handler, `POST /api/policies/documents`, which accepts
at most 26 MB (counted while reading, whatever `Content-Length` says), only from our own
origin (`Sec-Fetch-Site: same-origin` or a trusted `Origin`) and only with the
`X-Pairly-Upload: 1` header, which another site cannot send without a CORS preflight we
never answer.

## Design system

"Ledger and ink": warm paper surfaces, deep teal ink for primary actions, one amber signal
colour that always means "this needs you", hairline rules, a serif display face for
headings. Tokens are CSS variables in `src/app/globals.css`, redefined for dark mode.

Typefaces (all SIL Open Font License, self-hosted at build time by `next/font`):
Instrument Sans (UI), IBM Plex Sans Arabic (Arabic), Newsreader (headings), IBM Plex Mono
(keys and codes).

Rules: logical CSS properties only (`ms`/`me`, `ps`/`pe`, `start`/`end`) so RTL just works;
every string lives in `messages/*.json`; plain short sentences, never em dashes;
a visible focus ring on everything; animations switch off with `prefers-reduced-motion`.

Keyboard: `Cmd/Ctrl+K` opens the command palette, `G` then a letter jumps to a section
(`G H` home, `G I` inbox, `G E` expenses, `G R` hire, `G P` pay, `G A` audit, `G S` settings). A skip link
is the first focusable element on every page.

Storybook shows the shell, states, auth steps and primitives in English and Arabic, light
and dark (toolbar switches, plus pinned stories for each combination).

Storybook's own App Router wrapper (`nextjs.appDirectory: true` in
@storybook/nextjs-vite 10.6.1, the newest stable release) crashes every story under
Next 16.4. Stories therefore run without it, and `.storybook/app-router.tsx` provides
what `next/navigation` hooks read (router stand-in, pathname, search params, route
params; set them per story with `parameters.nextjs.navigation`). `pnpm test:storybook`
(also in CI) builds Storybook and renders every story with
`scripts/storybook-smoke.mjs`, so a crash like that fails the build instead of shipping.

# @pairly/web

The Pairly web app: Next.js (App Router, TypeScript strict), Tailwind CSS with shadcn/ui
style components on our own design tokens, Better Auth, next-intl (English and Arabic with
RTL), light and dark themes, Storybook.

The web app never decides access. It signs people in, gets a short-lived token from Better
Auth, and calls the FastAPI API with it. The API checks every permission. The sidebar reads
permissions from `/v1/me` only to hide links nobody could use.

## Run it

```bash
make dev                               # API, Postgres, Mailpit ... (default profile)
pnpm --filter @pairly/web auth:migrate # once: create the Better Auth tables (schema auth)
pnpm --filter @pairly/web dev          # http://localhost:3000, reads the repo-root .env
```

Or run everything in containers: `PROFILES=web make dev`. The `web` container applies the
Better Auth migrations itself on start (`PAIRLY_AUTH_MIGRATE_ON_START=true`).

Create an organisation with `make bootstrap-org` (see below), open the printed invite link,
then open Mailpit at http://localhost:8025 for the sign-in link.

Note: in compose the API fetches signing keys from `http://web:3000/api/auth/jwks`. If you
run the web app on the host with `pnpm dev` instead, point the API at it with
`PAIRLY_AUTH_JWKS_URL=http://host.docker.internal:3000/api/auth/jwks`, or use the `web`
profile.

Other scripts:

| Command | What it does |
|---|---|
| `pnpm --filter @pairly/web build` | Production build (`output: "standalone"`) |
| `pnpm --filter @pairly/web typecheck` | `next typegen` then `tsc` |
| `pnpm --filter @pairly/web storybook` | Storybook on port 6006 |
| `pnpm --filter @pairly/web auth:schema` | Print pending Better Auth SQL without applying it |
| `pnpm test` (repo root) | Vitest, web tests run in jsdom as the `web` project |
| `pnpm gen:api` (repo root) | Regenerate API types in `packages/shared-ts` |

## Layout

```
src/
  proxy.ts               CSP nonce, sign-in gate (fast path), locale routing
  instrumentation.ts     Better Auth migrations on start (when enabled)
  app/[locale]/(auth)    sign-in, check-email, sign-in/confirm, two-factor, invite
  app/[locale]/(app)     signed-in shell: home, inbox, hire, pay, audit, settings (members, security)
  app/api/auth/[...all]  Better Auth (magic link, 2FA, organizations, /token, /jwks)
  app/api/healthz        container liveness
  components/ui          primitives (button, input, dropdown, dialog, tooltip, kbd)
  components/shell       app shell, sidebar, org/locale/theme switchers, command palette
  components/states      action needed, not provisioned, unavailable, empty state
  components/auth        sign-in form, magic link confirm, 2FA verify and setup
  lib/auth               Better Auth config, 2FA gate, server helpers, browser client
  lib/api/server.ts      server-side API client (JWT + X-Pairly-Org)
  lib/provisioning.ts    /v1/me to viewer state
  lib/actions            server actions: invitations, organisation switch
  lib/org-mirror.ts      Better Auth organization mirror (same UUID as the API)
  lib/nav.ts             which sections the sidebar shows
messages/en.json, ar.json  every UI string
```

## Auth

- **Magic link** through SMTP (Mailpit locally). The email links to
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
(`G H` home, `G I` inbox, `G R` hire, `G P` pay, `G A` audit, `G S` settings). A skip link
is the first focusable element on every page.

Storybook shows the shell, states, auth steps and primitives in English and Arabic, light
and dark (toolbar switches, plus pinned stories for each combination).

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

Sign in with any email address, then open Mailpit at http://localhost:8025 for the link.

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
  app/[locale]/(auth)    sign-in, check-email, sign-in/confirm, two-factor
  app/[locale]/(app)     signed-in shell: home, inbox, hire, pay, audit, settings
  app/api/auth/[...all]  Better Auth (magic link, 2FA, organizations, /token, /jwks)
  app/api/healthz        container liveness
  components/ui          primitives (button, input, dropdown, dialog, tooltip, kbd)
  components/shell       app shell, sidebar, org/locale/theme switchers, command palette
  components/states      action needed, not provisioned, unavailable, empty state
  components/auth        sign-in form, magic link confirm, 2FA verify and setup
  lib/auth               Better Auth config, 2FA gate, server helpers, browser client
  lib/api/server.ts      server-side API client (JWT + X-Pairly-Org)
  lib/provisioning.ts    /v1/me to viewer state; provisioning placeholder
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
- **Organizations plugin** drives the org switcher. Users cannot create organizations from
  the web app (see the provisioning gap below).
- **JWT plugin**: EdDSA (Ed25519) tokens, 5 minute lifetime, keys at `/api/auth/jwks`.
  Issuer and audience come from `PAIRLY_AUTH_ISSUER` and `PAIRLY_AUTH_AUDIENCE`, the same
  variables the API checks. Tokens hold only `sub`, `iat`, `exp`, `iss` and `aud`. IDs are
  UUIDs, so an organization id goes to the API unchanged as `X-Pairly-Org`.
- **Database**: Postgres as role `pairly_auth`, whose search path is schema `auth`. It has
  no rights on the Pairly tables.
- **Protections**: secure, http-only, SameSite=Lax cookies (prefix `pairly`), Better Auth's
  origin checks on state-changing requests, rate limits stored in the database (magic link:
  5 per 5 minutes per IP; 2FA: 5 per minute), a nonce-based Content Security Policy, and
  standard security headers.

Settings are read when the server starts handling requests, never at build time. A missing
secret stops the server. In development, values the compose stack composes for the
container (`PAIRLY_AUTH_DATABASE_URL` and friends) are derived from the repo-root `.env`.

## Provisioning gap (decision needed)

Better Auth owns sign-in, its users and its organizations. The API owns `users`,
`organisations` and `memberships`, and maps a token to a user through
`users.auth_subject` (the Better Auth user id). **Nothing creates those API rows yet, and
the API has no endpoint for it.** So today every signed-in user gets `403` from `/v1/me`
and sees "Your account is not set up yet". That is the expected state for now.

The web side is ready for whichever design is picked:

- `lib/provisioning.ts` turns `/v1/me` into one `ViewerState` (`ready`,
  `not_provisioned`, `unauthenticated`, `unavailable`). Every page uses it.
- `requestProvisioning()` is a typed placeholder that throws. It is the only function to
  fill in once the API has an endpoint.

Options to decide between:

1. **Admin-driven**: an admin (or a seed script) creates the API organisation, the user and
   the membership, then adds the person to the matching Better Auth organization with the
   same UUID. Simple and explicit; needs an admin API and UI.
2. **Invitation-driven**: the API issues invitations (email + role + workflow); accepting
   one in the web app creates the Better Auth membership and calls an API endpoint that
   creates the user and membership. Best match for the brief's onboarding checklist.
3. **Just-in-time**: the API creates a `users` row on the first valid token and memberships
   come from invitations. Fewest steps, but users exist in the API before anyone vouched
   for them.

Until then, organizations cannot be created from the web app
(`allowUserToCreateOrganization: false`), so Better Auth and the API cannot drift apart.

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

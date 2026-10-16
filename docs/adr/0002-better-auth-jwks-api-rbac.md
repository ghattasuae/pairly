# ADR 0002: Better Auth in the web app, JWKS verification and RBAC in the API

Status: accepted

Date: 2026-10-07

## Context

Staff sign in with a magic link or Google or Microsoft SSO, with TOTP as a second factor. Candidates and vendors sign in to portals. Authorisation must be checked on the server, on every request and every agent tool call (brief 6.1, 6.2), and must not depend on the web app.

## Decision

* Better Auth runs in the Next.js app. It handles sign-in, sessions, SSO and TOTP, and issues short-lived JWTs.
* The API verifies each JWT against Better Auth's JWKS endpoint (`pairly_core.auth.TokenVerifier`): asymmetric algorithms from an allowlist only, `kid` required, issuer, audience and expiry checked. Keys are cached, with one rate-limited refetch for an unknown key id. If the keys cannot be fetched, verification fails closed.
* All RBAC lives in the API (`pairly_core.rbac`, `pairly_core.identity`). The web app hides what a person cannot do, but the API decides. The token's `amr` claim says whether the sign-in included a TOTP code; the API uses it for the step-up rule ([ADR 0014](0014-step-up-on-commitments.md)).
* API keys (`pk_...`) are a second way in, resolved by the API to an `api_key` actor with fixed limits: read, propose and upload only.

## Consequences

* The API can be used without the web app (SDKs, MCP server), with the same checks.
* A malformed or missing `amr` reads as no second factor, so the step-up rule fails closed.
* Key rotation in Better Auth needs no API restart.

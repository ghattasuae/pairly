// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Better Auth endpoints that browsers must not reach.
 *
 * - `/token` mints API tokens. Only the Next server mints them, in-process through
 *   `auth.api.getToken` (lib/auth/server.ts), never over HTTP, so a script running in a
 *   page (an XSS) cannot get a bearer token. `/jwks` stays public: the API needs it.
 * - `/organization/*`: Better Auth organizations only mirror the API's organisations for
 *   the session's active organisation. The API owns membership (invitations), and the
 *   server changes the mirror in-process (lib/org-mirror.ts). Over HTTP these endpoints
 *   would offer a second, unaudited invitation and membership system.
 *
 * `disabledPaths: ["/token"]` in the auth config blocks the first one inside Better Auth
 * too; this list is checked first, in the route handler.
 */
const BLOCKED_EXACT = new Set(["/token"]);
const BLOCKED_PREFIXES = ["/organization/"];

/** `pathname` is the request path, for example `/api/auth/token`. */
export function isBlockedAuthPath(pathname: string, basePath = "/api/auth"): boolean {
  if (!pathname.startsWith(`${basePath}/`)) return false;
  const rest = pathname.slice(basePath.length).replace(/\/+$/, "") || "/";
  const normalised = rest.toLowerCase();
  return BLOCKED_EXACT.has(normalised) || BLOCKED_PREFIXES.some((p) => `${normalised}/`.startsWith(p));
}

/**
 * Hosted demo only (`PAIRLY_DEMO_PUBLIC_SIGNIN`, lib/demo/policy.ts): many visitors share
 * each demo account, so no visitor may change the account for the others. Turning
 * two-factor on would lock everybody else out until the nightly reset; changing the
 * address, the name or the linked accounts, deleting the account and signing the other
 * sessions out are closed the same way.
 */
const DEMO_BLOCKED_EXACT = new Set([
  "/two-factor/enable",
  "/two-factor/disable",
  "/two-factor/generate-backup-codes",
  "/change-email",
  "/update-user",
  "/delete-user",
  "/revoke-sessions",
  "/revoke-other-sessions",
  "/link-social",
  "/unlink-account",
]);

export function isDemoBlockedAuthPath(pathname: string, demoSignin: boolean, basePath = "/api/auth"): boolean {
  if (!demoSignin || !pathname.startsWith(`${basePath}/`)) return false;
  const rest = (pathname.slice(basePath.length).replace(/\/+$/, "") || "/").toLowerCase();
  return DEMO_BLOCKED_EXACT.has(rest) || rest.startsWith("/delete-user/");
}

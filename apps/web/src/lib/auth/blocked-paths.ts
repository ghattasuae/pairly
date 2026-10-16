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

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * How a session was signed in, carried to the API as the JWT `amr` claim (RFC 8176).
 *
 * Better Auth records the value on the session row when the session is created, keyed on
 * the endpoint that created it:
 *
 * - `/two-factor/verify-totp`: `otp`. A valid TOTP code was entered (at sign-in, or while
 *   turning 2FA on, which replaces the session).
 * - `/magic-link/verify`: `email`. For a user with 2FA on, `twoFactorGate` deletes this
 *   session again and the real one comes from `/two-factor/verify-totp`.
 * - `/two-factor/verify-backup-code`: `backup`. A one-time backup code is a second factor,
 *   so the claim is `["otp", "backup"]`: the API only checks for `otp`, and `backup` keeps
 *   the difference visible. The sign-in is logged and the user sees how many codes remain.
 * - SSO callbacks: `sso`.
 * - Anything else (turning 2FA off, an unknown future path): `none`.
 *   Fail closed: only `otp` unlocks approve, decide, manage and export in the API.
 */
export type SessionAmr = "otp" | "backup" | "email" | "sso" | "none";

const AMR_BY_PATH: Readonly<Record<string, SessionAmr>> = {
  "/two-factor/verify-totp": "otp",
  "/two-factor/verify-backup-code": "backup",
  "/magic-link/verify": "email",
  "/callback/:id": "sso",
  "/oauth2/callback/:id": "sso",
};

export function amrForPath(path: string | undefined | null): SessionAmr {
  return (path && Object.hasOwn(AMR_BY_PATH, path) ? AMR_BY_PATH[path] : undefined) ?? "none";
}

/** The `amr` claim for a session's stored value. Unknown or missing values become `none`. */
export function amrClaim(stored: unknown): string[] {
  if (stored === "backup") return ["otp", "backup"];
  const known: readonly SessionAmr[] = ["otp", "email", "sso"];
  return [known.includes(stored as SessionAmr) ? (stored as SessionAmr) : "none"];
}

/** Whether a session was signed in with a backup code (shown as a notice). */
export function signedInWithBackupCode(session: unknown): boolean {
  return (session as { amr?: unknown } | null | undefined)?.amr === "backup";
}

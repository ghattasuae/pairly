// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** What a server action returns to the browser: a small payload or a stable error code. */
export type ActionResult<T extends object = object> = ({ ok: true } & T) | { ok: false; code: string };

/** Codes the UI explains in its own words. Anything else reads as a generic failure. */
export const KNOWN_ACTION_CODES = new Set([
  "not_signed_in",
  "permission_denied",
  "second_factor_required",
  "not_found",
  "conflict",
  "gone",
  "invalid_input",
  "rate_limited",
  "unavailable",
  "already_decided",
  "approval_state_conflict",
  "segregation_of_duties",
  "not_assigned",
  "idempotency_key_reused",
  "too_large",
  "test_key_not_allowed",
]);

/** The message key for an action error code (`generic` when unknown). */
export function errorKey(code: string): string {
  return KNOWN_ACTION_CODES.has(code) ? code : "generic";
}

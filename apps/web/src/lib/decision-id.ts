// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** Decision record ids: `dec_` plus a 26 character ULID (Crockford base32), as the API checks. */
export function isDecisionId(value: unknown): value is string {
  return typeof value === "string" && /^dec_[0-9A-HJKMNP-TV-Z]{26}$/.test(value);
}

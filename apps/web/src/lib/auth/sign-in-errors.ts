// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

export type SignInErrorKey = "invalidEmail" | "rateLimited" | "generic" | "originMismatch";

/**
 * Better Auth rejects requests from an origin it does not trust with 403 INVALID_ORIGIN.
 * In development that is almost always 127.0.0.1 versus localhost, so say so plainly.
 * Production keeps the generic message: it reveals nothing about the configuration.
 */
export function errorKeyFor(
  result: { status?: number | undefined; code?: string | undefined },
  dev: boolean = process.env.NODE_ENV === "development",
): SignInErrorKey {
  if (result.status === 429) return "rateLimited";
  if (dev && result.code === "INVALID_ORIGIN") return "originMismatch";
  return "generic";
}

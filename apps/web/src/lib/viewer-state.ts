// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** How a `/v1/me` response maps to what the web app shows. Pure, no I/O. */
import type { Me } from "@pairly/shared-ts";

export type ViewerState =
  /** The API knows the user in the active org. `me.permissions` drives what the nav shows. */
  | { status: "ready"; me: Me }
  /** Signed in, token accepted, but the API has no user or membership for them (403). */
  | { status: "not_provisioned"; code: string }
  /** The API rejected the token (401). Should not happen; treated as signed out. */
  | { status: "unauthenticated" }
  /** The API is down, slow or answered with something unexpected. Fail closed. */
  | { status: "unavailable" };

interface ProblemLike {
  code?: unknown;
}

export function classifyMeResponse(
  status: number | undefined,
  data: Me | undefined,
  error: unknown,
): ViewerState {
  if (status === 200 && data) return { status: "ready", me: data };
  if (status === 401) return { status: "unauthenticated" };
  if (status === 403) {
    const code = (error as ProblemLike | undefined)?.code;
    return { status: "not_provisioned", code: typeof code === "string" ? code : "forbidden" };
  }
  return { status: "unavailable" };
}

/**
 * Second factor state for the "set up two-factor to continue" notice. `null` when the user
 * holds no permission that needs it, or already signed in with a TOTP code.
 *
 * - `setup`: TOTP is off. They must turn it on (Account, Security).
 * - `reauth`: TOTP is on, but this session started without a code (it was turned on
 *   later, or a backup code was used). Signing in again fixes it.
 */
export function secondFactorNeed(me: Me, totpEnabled: boolean): "setup" | "reauth" | null {
  if (me.second_factor || me.second_factor_permissions.length === 0) return null;
  return totpEnabled ? "reauth" : "setup";
}

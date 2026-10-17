// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** Server-side session helpers for React Server Components, server actions and routes. */
import "server-only";

import { headers } from "next/headers";
import { cache } from "react";

import { getAuth } from "./config";

export { getAuth } from "./config";

/** The Better Auth session for this request, or null. Cached per request. */
export const getSession = cache(async () => {
  return getAuth().api.getSession({ headers: await headers() });
});

export type Session = NonNullable<Awaited<ReturnType<typeof getSession>>>;

/**
 * A fresh short-lived API token (JWT) for the signed-in user. Cached per request.
 *
 * Minted in-process (`auth.api.getToken` calls the endpoint function directly). The HTTP
 * route `/api/auth/token` is closed, so browsers can never obtain a token.
 */
export const getApiToken = cache(async (): Promise<string | null> => {
  const session = await getSession();
  if (!session) return null;
  const { token } = await getAuth().api.getToken({ headers: await headers() });
  return token;
});

/** Whether the signed-in user has turned TOTP on (Better Auth two-factor plugin). */
export function twoFactorEnabled(session: Session | null): boolean {
  return Boolean((session?.user as { twoFactorEnabled?: boolean | null } | undefined)?.twoFactorEnabled);
}

/** Backup codes the user has left, or null if unknown. Count only; codes never leave here. */
export async function remainingBackupCodes(userId: string): Promise<number | null> {
  try {
    const result = await getAuth().api.viewBackupCodes({ body: { userId } });
    return Array.isArray(result.backupCodes) ? result.backupCodes.length : null;
  } catch {
    return null;
  }
}

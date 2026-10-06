// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Server-side session helpers for React Server Components and route handlers. */
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

/** A fresh short-lived API token (JWT) for the signed-in user. Cached per request. */
export const getApiToken = cache(async (): Promise<string | null> => {
  const session = await getSession();
  if (!session) return null;
  const { token } = await getAuth().api.getToken({ headers: await headers() });
  return token;
});

/** Organizations the user belongs to in Better Auth (for the org switcher). */
export const listOrganizations = cache(async () => {
  const session = await getSession();
  if (!session) return [];
  return getAuth().api.listOrganizations({ headers: await headers() });
});

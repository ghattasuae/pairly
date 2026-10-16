// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Server-side client for the FastAPI API.
 *
 * Every call carries a fresh short-lived JWT from Better Auth (`Authorization: Bearer`)
 * and, for organisation-scoped calls, the active organisation (`X-Pairly-Org`). The API
 * verifies the token against our JWKS and makes every authorization decision. This module
 * never decides access.
 *
 * The active organisation comes from `GET /v1/me/orgs` (the API's list of the user's
 * memberships): the session's choice if it is still in that list, otherwise the first
 * one, which is then mirrored into Better Auth (lib/org-mirror.ts).
 *
 * Server only: tokens never reach the browser through this path.
 */
import "server-only";

import { ORG_HEADER, type ApiPaths, type OrgSummary } from "@pairly/shared-ts";
import createClient, { type Client } from "openapi-fetch";
import { cache } from "react";

import { getApiToken, getSession } from "../auth/server";
import { serverEnv } from "../env";
import { activateOrganization } from "../org-mirror";

export type ApiClient = Client<ApiPaths>;

/** Milliseconds before an API call is abandoned. Pages must stay fast (brief 5.3). */
export const API_TIMEOUT_MS = 5000;

export class NotSignedInError extends Error {
  constructor() {
    super("not signed in");
    this.name = "NotSignedInError";
  }
}

export function buildApiHeaders(token: string | null, orgId: string | null | undefined): Headers {
  const headers = new Headers({ Accept: "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (orgId) headers.set(ORG_HEADER, orgId);
  return headers;
}

function createApi(token: string | null, orgId: string | null): ApiClient {
  return createClient<ApiPaths>({
    baseUrl: serverEnv().apiInternalUrl,
    headers: buildApiHeaders(token, orgId),
    cache: "no-store",
    redirect: "error",
    fetch: (request: Request) => fetch(request, { signal: AbortSignal.timeout(API_TIMEOUT_MS) }),
  });
}

/** No token at all: only for `POST /v1/invitations/preview`. */
export function getPublicApiClient(): ApiClient {
  return createApi(null, null);
}

/** Signed in, no organisation: `/v1/me/orgs` and `/v1/invitations/accept`. */
export const getUserApiClient = cache(async (): Promise<ApiClient> => {
  const token = await getApiToken();
  if (!token) throw new NotSignedInError();
  return createApi(token, null);
});

/** The user's organisations according to the API, or null if the API did not answer. */
export const listMyOrgs = cache(async (): Promise<OrgSummary[] | null> => {
  try {
    const { data, response } = await (await getUserApiClient()).GET("/v1/me/orgs");
    return response.status === 200 && data ? data.items : null;
  } catch (err) {
    if (err instanceof NotSignedInError) throw err;
    return null;
  }
});

/** The organisation this request works in (see the module docstring), or null. */
export const getActiveOrgId = cache(async (): Promise<string | null> => {
  const session = await getSession();
  if (!session) throw new NotSignedInError();
  const chosen = (session.session as { activeOrganizationId?: string | null }).activeOrganizationId ?? null;
  const orgs = await listMyOrgs();
  // API unreachable: keep the session's choice; the API still decides on every call.
  if (orgs === null) return chosen;
  if (chosen && orgs.some((org) => org.id === chosen)) return chosen;
  const first = orgs[0];
  if (!first) return null;
  try {
    await activateOrganization(first, session.user.id);
  } catch {
    // Best effort: this request still uses `first`; the next one tries again.
  }
  return first.id;
});

/**
 * An API client bound to the signed-in user and their active organisation, built once per
 * request. Throws `NotSignedInError` without a session.
 */
export const getApiClient = cache(async (): Promise<ApiClient> => {
  const token = await getApiToken();
  if (!token) throw new NotSignedInError();
  return createApi(token, await getActiveOrgId());
});

/** The `code` of a problem+json error body, if there is one. */
export function problemCode(error: unknown): string | null {
  const code = (error as { code?: unknown } | undefined)?.code;
  return typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? code : null;
}

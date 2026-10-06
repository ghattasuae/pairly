// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Server-side client for the FastAPI API.
 *
 * Every call carries a fresh short-lived JWT from Better Auth (`Authorization: Bearer`)
 * and the active organisation (`X-Pairly-Org`). The API verifies the token against our
 * JWKS and makes every authorization decision. This module never decides access.
 *
 * Server only: tokens never reach the browser through this path.
 */
import "server-only";

import { ORG_HEADER, type ApiPaths } from "@pairly/shared-ts";
import createClient, { type Client } from "openapi-fetch";
import { cache } from "react";

import { getApiToken, getSession } from "../auth/server";
import { serverEnv } from "../env";

export type ApiClient = Client<ApiPaths>;

/** Milliseconds before an API call is abandoned. Pages must stay fast (brief 5.3). */
export const API_TIMEOUT_MS = 5000;

export class NotSignedInError extends Error {
  constructor() {
    super("not signed in");
    this.name = "NotSignedInError";
  }
}

export function buildApiHeaders(token: string, orgId: string | null | undefined): Headers {
  const headers = new Headers({ Authorization: `Bearer ${token}`, Accept: "application/json" });
  if (orgId) headers.set(ORG_HEADER, orgId);
  return headers;
}

/**
 * An API client bound to the signed-in user and their active organisation, built once per
 * request. Throws `NotSignedInError` without a session.
 */
export const getApiClient = cache(async (): Promise<ApiClient> => {
  const session = await getSession();
  const token = await getApiToken();
  if (!session || !token) throw new NotSignedInError();
  const orgId = (session.session as { activeOrganizationId?: string | null }).activeOrganizationId;
  return createClient<ApiPaths>({
    baseUrl: serverEnv().apiInternalUrl,
    headers: buildApiHeaders(token, orgId),
    cache: "no-store",
    redirect: "error",
    fetch: (request: Request) => fetch(request, { signal: AbortSignal.timeout(API_TIMEOUT_MS) }),
  });
});

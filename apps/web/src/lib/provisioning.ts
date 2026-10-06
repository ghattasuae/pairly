// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Provisioning: does the API know this Better Auth user in the active organisation?
 *
 * PROVISIONING GAP (decision needed, see apps/web/README.md). Better Auth owns sign-in,
 * users and organizations; the API owns `users`, `organisations` and `memberships`, and
 * maps a token to a user by `users.auth_subject` (the Better Auth user id). Nothing creates
 * the API rows yet, and the API has no endpoint for it. Until that exists, every new user
 * gets 403 from `/v1/me` and sees the "not provisioned" state.
 *
 * `resolveViewer` is the single place the web app turns `/v1/me` into a state, so wiring
 * up provisioning later changes only `requestProvisioning` below and nothing else.
 */
import "server-only";

import { cache } from "react";

import { getApiClient, NotSignedInError } from "./api/server";
import { classifyMeResponse, type ViewerState } from "./viewer-state";

export type { ViewerState } from "./viewer-state";

/** What the signed-in user may see, according to the API. Cached per request. */
export const resolveViewer = cache(async (): Promise<ViewerState> => {
  try {
    const client = await getApiClient();
    const { data, error, response } = await client.GET("/v1/me");
    return classifyMeResponse(response.status, data, error);
  } catch (err) {
    if (err instanceof NotSignedInError) return { status: "unauthenticated" };
    return { status: "unavailable" };
  }
});

/**
 * PLACEHOLDER(provisioning): ask the API to create the user and membership for the
 * current Better Auth user and organization. Not implemented: the API has no endpoint
 * for it yet, and the design is a decision for the project owner (see README). Calling
 * it fails loudly so nobody mistakes it for working code.
 */
export async function requestProvisioning(): Promise<never> {
  throw new Error("Provisioning is not implemented: the API has no provisioning endpoint yet.");
}

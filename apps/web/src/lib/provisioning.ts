// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Provisioning: does the API know this user in the active organisation?
 *
 * People join an organisation only by accepting an invitation (`/[locale]/invite/[token]`,
 * see apps/web/README.md). Until then `/v1/me` answers 403 and pages show the "not set
 * up" state, which tells them to ask their admin for an invite.
 *
 * `resolveViewer` is the single place the web app turns `/v1/me` into a state.
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

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Finish the Slack connect flow (`/pairly connect` in Slack, then this page): links the
 * Slack user the signed token names to the signed-in Pairly user, in the organisation the
 * token belongs to (the API checks they are a member). One answer for a used, expired,
 * forged or other organisation's token.
 */
import type { ActionResult } from "../action-result";
import { callApi } from "../api/call";
import { getSession } from "../auth/server";
import { isSlackToken } from "../slack-connect-token";

export async function confirmSlackConnect(token: string): Promise<ActionResult> {
  if (!isSlackToken(token)) return { ok: false, code: "gone" };
  if (!(await getSession())) return { ok: false, code: "not_signed_in" };
  const result = await callApi(
    (api) => api.POST("/v1/integrations/slack/connect/confirm", { body: { token } }),
    { scope: "user" },
  );
  if (result.ok) return { ok: true };
  if (result.status === 410 || result.status === 404 || result.status === 403) return { ok: false, code: "gone" };
  return { ok: false, code: result.code };
}

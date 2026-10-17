// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the unsubscribe landing page. No sign-in: the signed token is the
 * proof, and the API decides everything. Looking at a link changes nothing; only the
 * explicit confirmation sends the RFC 8058 one-click POST.
 */
import { getPublicApiClient } from "../api/server";
import { isWellFormedUnsubscribeToken, type UnsubscribeView } from "../unsubscribe";

export async function describeUnsubscribe(token: string): Promise<UnsubscribeView> {
  if (!isWellFormedUnsubscribeToken(token)) return { kind: "invalid" };
  try {
    const { data, response } = await getPublicApiClient().GET("/v1/notifications/unsubscribe", {
      params: { query: { token } },
    });
    if (response.status !== 200 || !data) return { kind: "unavailable" };
    if (data.state === "valid" && data.category) return { kind: "valid", category: data.category };
    return data.state === "expired" ? { kind: "expired" } : { kind: "invalid" };
  } catch {
    return { kind: "unavailable" };
  }
}

export async function confirmUnsubscribe(token: string): Promise<{ ok: boolean }> {
  if (!isWellFormedUnsubscribeToken(token)) return { ok: false };
  try {
    const { response } = await getPublicApiClient().POST("/v1/notifications/unsubscribe", {
      params: { query: { token } },
    });
    return { ok: response.status === 204 };
  } catch {
    return { ok: false };
  }
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for a candidate's communication preferences (Pairly Hire). No sign-in:
 * the signed token from their email is the proof, and the API decides everything. The
 * token travels in the JSON body, never in a URL.
 */
import { headers } from "next/headers";

import { getPublicApiClient } from "../api/server";
import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import type { HirePrefs } from "../hire/types";

const TOKEN_RE = /^hp1\.[0-9a-f]{32}\.[0-9a-f]{32}\.\d{1,12}\.[A-Za-z0-9_-]{43}$/;

export type PrefsView = { kind: "ready"; prefs: HirePrefs } | { kind: "invalid" } | { kind: "unavailable" };

async function forwarded(): Promise<Record<string, string>> {
  const ip = clientIpFrom(await headers());
  return ip ? { [CLIENT_IP_HEADER]: ip } : {};
}

export async function readCandidatePreferences(token: string): Promise<PrefsView> {
  if (!TOKEN_RE.test(token)) return { kind: "invalid" };
  try {
    const { data, response } = await getPublicApiClient().POST("/v1/hire/public/preferences/read", {
      body: { token },
      headers: await forwarded(),
    });
    if (response.status === 404) return { kind: "invalid" };
    if (response.status !== 200 || !data) return { kind: "unavailable" };
    return { kind: "ready", prefs: data };
  } catch {
    return { kind: "unavailable" };
  }
}

export async function saveCandidatePreferences(
  token: string,
  changes: { updates_email?: boolean; talent_pool?: boolean },
): Promise<PrefsView> {
  if (!TOKEN_RE.test(token)) return { kind: "invalid" };
  const body: { token: string; updates_email?: boolean; talent_pool?: boolean } = { token };
  if (typeof changes.updates_email === "boolean") body.updates_email = changes.updates_email;
  if (typeof changes.talent_pool === "boolean") body.talent_pool = changes.talent_pool;
  try {
    const { data, response } = await getPublicApiClient().POST("/v1/hire/public/preferences", {
      body,
      headers: await forwarded(),
    });
    if (response.status === 404) return { kind: "invalid" };
    if (response.status !== 200 || !data) return { kind: "unavailable" };
    return { kind: "ready", prefs: data };
  } catch {
    return { kind: "unavailable" };
  }
}

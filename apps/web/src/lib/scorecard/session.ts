// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The interviewer's scorecard session (M3, brief 5.3), on the server only.
 *
 * The one-click link in the briefing, the calendar invitation or a reminder opens a
 * scorecard session at the API (`POST /v1/hire/public/scorecards/open`): one interview,
 * one interviewer, 12 hours. Its token lives in an HTTP-only cookie of its own
 * (`__Host-pairly_scorecard` over HTTPS, `pairly_scorecard` in local development),
 * SameSite Lax, never readable by scripts, and goes to the API only in the
 * `X-Pairly-Scorecard-Session` header. It is never a staff credential.
 */
import "server-only";

import { cookies, headers } from "next/headers";

import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import { serverEnv } from "../env";

export const SCORECARD_HEADER = "X-Pairly-Scorecard-Session";
const TIMEOUT_MS = 8000;
const TOKEN_RE = /^ss1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;

function secure(): boolean {
  return serverEnv().baseUrl.startsWith("https://");
}

export function scorecardCookieName(): string {
  return secure() ? "__Host-pairly_scorecard" : "pairly_scorecard";
}

export async function readScorecardToken(): Promise<string | null> {
  const value = (await cookies()).get(scorecardCookieName())?.value ?? null;
  return value && TOKEN_RE.test(value) ? value : null;
}

export async function setScorecardToken(token: string, expiresAt: string): Promise<void> {
  if (!TOKEN_RE.test(token)) throw new Error("invalid scorecard token");
  const expires = new Date(expiresAt);
  (await cookies()).set(scorecardCookieName(), token, {
    httpOnly: true,
    secure: secure(),
    sameSite: "lax",
    path: "/",
    expires: Number.isNaN(expires.getTime()) ? undefined : expires,
  });
}

export type ScorecardResult<T> = { ok: true; data: T } | { ok: false; code: string };

/** One call to the scorecard API (`session: true` sends the scorecard session). */
export async function scorecardCall<T>(
  path: `/v1/hire/${string}`,
  init: { method?: string; body?: unknown; session?: boolean } = {},
): Promise<ScorecardResult<T>> {
  const { method = "GET", body, session = true } = init;
  const request = new Headers({ Accept: "application/json" });
  const ip = clientIpFrom(await headers());
  if (ip) request.set(CLIENT_IP_HEADER, ip);
  if (session) {
    const token = await readScorecardToken();
    if (!token) return { ok: false, code: "not_signed_in" };
    request.set(SCORECARD_HEADER, token);
  }
  if (body !== undefined) request.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`${serverEnv().apiInternalUrl}${path}`, {
      method,
      headers: request,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    return { ok: false, code: "unavailable" };
  }
  if (response.status !== 200) {
    const codes: Record<number, string> = {
      400: "invalid_input",
      401: "not_signed_in",
      404: "not_found",
      409: "conflict",
      422: "invalid_input",
      429: "rate_limited",
    };
    return { ok: false, code: codes[response.status] ?? "unavailable" };
  }
  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

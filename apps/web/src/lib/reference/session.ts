// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The referee's session (M3, brief 5.1.6), on the server only.
 *
 * The signed single use link in the referee's email opens a referee session at the API
 * (`POST /v1/hire/public/references/open`): one reference request, 24 hours. Its token
 * lives in an HTTP-only cookie of its own (`__Host-pairly_reference` over HTTPS,
 * `pairly_reference` in local development), SameSite Lax, never readable by scripts, and
 * goes to the API only in the `X-Pairly-Reference-Session` header. A referee is never a
 * user: the session opens that one form and nothing else.
 */
import "server-only";

import { cookies, headers } from "next/headers";

import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import { serverEnv } from "../env";

export const REFERENCE_HEADER = "X-Pairly-Reference-Session";
const TIMEOUT_MS = 8000;
const TOKEN_RE = /^rs1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;

function secure(): boolean {
  return serverEnv().baseUrl.startsWith("https://");
}

export function referenceCookieName(): string {
  return secure() ? "__Host-pairly_reference" : "pairly_reference";
}

export async function readReferenceToken(): Promise<string | null> {
  const value = (await cookies()).get(referenceCookieName())?.value ?? null;
  return value && TOKEN_RE.test(value) ? value : null;
}

export async function setReferenceToken(token: string, expiresAt: string): Promise<void> {
  if (!TOKEN_RE.test(token)) throw new Error("invalid reference token");
  const expires = new Date(expiresAt);
  (await cookies()).set(referenceCookieName(), token, {
    httpOnly: true,
    secure: secure(),
    sameSite: "lax",
    path: "/",
    expires: Number.isNaN(expires.getTime()) ? undefined : expires,
  });
}

export type PublicResult<T> = { ok: true; data: T } | { ok: false; code: string };

const CODES: Record<number, string> = {
  400: "invalid_input",
  401: "not_signed_in",
  404: "not_found",
  409: "conflict",
  422: "invalid_input",
  429: "rate_limited",
};

/** One call to a public Hire API route (`session: true` sends the referee session). */
export async function publicHireCall<T>(
  path: `/v1/hire/${string}`,
  init: { method?: string; body?: unknown; session?: boolean } = {},
): Promise<PublicResult<T>> {
  const { method = "GET", body, session = false } = init;
  const request = new Headers({ Accept: "application/json" });
  const ip = clientIpFrom(await headers());
  if (ip) request.set(CLIENT_IP_HEADER, ip);
  if (session) {
    const token = await readReferenceToken();
    if (!token) return { ok: false, code: "not_signed_in" };
    request.set(REFERENCE_HEADER, token);
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
  if (response.status !== 200 && response.status !== 201) {
    return { ok: false, code: CODES[response.status] ?? "unavailable" };
  }
  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

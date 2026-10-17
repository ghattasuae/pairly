// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The vendor's onboarding session (WP4.2, brief 4.1.1), on the server only.
 *
 * The signed single use invite link opens an onboarding session at the API
 * (`POST /v1/pay/public/vendor-onboarding/open`): one invite, 24 hours. Its token lives
 * in an HTTP-only cookie of its own (`__Host-pairly_vendor` over HTTPS, `pairly_vendor`
 * in local development), SameSite Lax, never readable by scripts, and goes to the API
 * only in the `X-Pairly-Vendor-Session` header. A vendor is never a user: the session
 * can add documents and submit the form, nothing else.
 */
import "server-only";

import { cookies, headers } from "next/headers";

import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import { serverEnv } from "../env";

export const VENDOR_HEADER = "X-Pairly-Vendor-Session";
const TIMEOUT_MS = 8000;
const TOKEN_RE = /^vs1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;

function secure(): boolean {
  return serverEnv().baseUrl.startsWith("https://");
}

export function vendorCookieName(): string {
  return secure() ? "__Host-pairly_vendor" : "pairly_vendor";
}

export async function readVendorToken(): Promise<string | null> {
  const value = (await cookies()).get(vendorCookieName())?.value ?? null;
  return value && TOKEN_RE.test(value) ? value : null;
}

export async function setVendorToken(token: string, expiresAt: string): Promise<void> {
  if (!TOKEN_RE.test(token)) throw new Error("invalid vendor session token");
  const expires = new Date(expiresAt);
  (await cookies()).set(vendorCookieName(), token, {
    httpOnly: true,
    secure: secure(),
    sameSite: "lax",
    path: "/",
    expires: Number.isNaN(expires.getTime()) ? undefined : expires,
  });
}

export async function clearVendorToken(): Promise<void> {
  (await cookies()).delete(vendorCookieName());
}

export type PublicResult<T> = { ok: true; data: T } | { ok: false; code: string };

const CODES: Record<number, string> = {
  400: "invalid_input",
  401: "not_signed_in",
  404: "not_found",
  409: "conflict",
  413: "too_large",
  415: "invalid_input",
  422: "invalid_input",
  429: "rate_limited",
};

async function baseHeaders(session: boolean): Promise<Headers | null> {
  const request = new Headers({ Accept: "application/json" });
  const ip = clientIpFrom(await headers());
  if (ip) request.set(CLIENT_IP_HEADER, ip);
  if (session) {
    const token = await readVendorToken();
    if (!token) return null;
    request.set(VENDOR_HEADER, token);
  }
  return request;
}

/** One call to a public Pay onboarding route (`session: true` sends the vendor session). */
export async function publicPayCall<T>(
  path: `/v1/pay/public/${string}` | `/v1/pay/vendor-onboarding/${string}`,
  init: { method?: string; body?: unknown; session?: boolean } = {},
): Promise<PublicResult<T>> {
  const { method = "GET", body, session = false } = init;
  const request = await baseHeaders(session);
  if (!request) return { ok: false, code: "not_signed_in" };
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
    let code = CODES[response.status] ?? "unavailable";
    try {
      const problem = (await response.json()) as { code?: string };
      if (problem.code === "bank_details_invalid" || problem.code === "trn_invalid" || problem.code === "phone_invalid") {
        code = problem.code;
      }
    } catch {
      // keep the status code
    }
    return { ok: false, code };
  }
  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

/** A raw call with the vendor session (document upload). `null` without a session. */
export async function vendorUpstream(
  path: `/v1/pay/vendor-onboarding/${string}`,
  init: { method?: string; body?: BodyInit; timeoutMs?: number },
): Promise<Response | null> {
  const request = await baseHeaders(true);
  if (!request) return null;
  return fetch(`${serverEnv().apiInternalUrl}${path}`, {
    method: init.method ?? "GET",
    headers: request,
    body: init.body ?? null,
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(init.timeoutMs ?? TIMEOUT_MS),
  });
}

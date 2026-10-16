// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The vendor portal's session, on the server only (brief 4.7).
 *
 * A vendor's contacts are not staff: they never get a Better Auth session. A magic link
 * from their email opens a portal session at the API (`POST /v1/pay/vendor-portal/session`);
 * its token lives in an HTTP-only cookie of its own (`__Host-pairly_vendor_portal` over
 * HTTPS, bound to this host and path `/`; `pairly_vendor_portal` in local development),
 * SameSite Lax, never readable by scripts, and separate from the candidate portal's. Only
 * the vendor portal code reads it, and only to send it to the API in the
 * `X-Pairly-Vendor-Portal` header. The API never accepts it as a staff credential.
 */
import "server-only";

import { cookies, headers } from "next/headers";

import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import { serverEnv } from "../env";
import { SESSION_RE } from "./types";

export const VENDOR_PORTAL_HEADER = "X-Pairly-Vendor-Portal";
export const VENDOR_PORTAL_TIMEOUT_MS = 5000;

function secure(): boolean {
  return serverEnv().baseUrl.startsWith("https://");
}

export function vendorPortalCookieName(): string {
  return secure() ? "__Host-pairly_vendor_portal" : "pairly_vendor_portal";
}

export async function readVendorPortalToken(): Promise<string | null> {
  const value = (await cookies()).get(vendorPortalCookieName())?.value ?? null;
  return value && SESSION_RE.test(value) ? value : null;
}

export async function setVendorPortalToken(token: string, expiresAt: string): Promise<void> {
  if (!SESSION_RE.test(token)) throw new Error("invalid vendor portal token");
  const expires = new Date(expiresAt);
  (await cookies()).set(vendorPortalCookieName(), token, {
    httpOnly: true,
    secure: secure(),
    sameSite: "lax",
    path: "/",
    expires: Number.isNaN(expires.getTime()) ? undefined : expires,
  });
}

export async function clearVendorPortalToken(): Promise<void> {
  (await cookies()).delete(vendorPortalCookieName());
}

export type VendorPortalResult<T> = { ok: true; data: T; status: number } | { ok: false; code: string; status: number | null };

async function forwardedIp(): Promise<Record<string, string>> {
  const ip = clientIpFrom(await headers());
  return ip ? { [CLIENT_IP_HEADER]: ip } : {};
}

function codeFor(status: number): string {
  if (status === 401) return "not_signed_in";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 400 || status === 422) return "invalid_input";
  if (status === 413) return "too_large";
  return "unavailable";
}

/**
 * One call to the vendor portal API. `session: true` sends the contact's portal session
 * (and answers `not_signed_in` without one). Never follows redirects; short timeout.
 */
export async function vendorPortalCall<T>(
  path: `/v1/pay/vendor-portal/${string}`,
  init: { method?: string; body?: unknown; session?: boolean; okStatuses?: number[] } = {},
): Promise<VendorPortalResult<T>> {
  const { method = "GET", body, session = true, okStatuses = [200, 201, 202, 204] } = init;
  const request = new Headers({ Accept: "application/json", ...(await forwardedIp()) });
  if (session) {
    const token = await readVendorPortalToken();
    if (!token) return { ok: false, code: "not_signed_in", status: null };
    request.set(VENDOR_PORTAL_HEADER, token);
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
      signal: AbortSignal.timeout(VENDOR_PORTAL_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, code: "unavailable", status: null };
  }
  if (!okStatuses.includes(response.status)) return { ok: false, code: codeFor(response.status), status: response.status };
  if (response.status === 204) return { ok: true, data: undefined as T, status: 204 };
  try {
    return { ok: true, data: (await response.json()) as T, status: response.status };
  } catch {
    return { ok: false, code: "unavailable", status: response.status };
  }
}

/** Raw upstream request with the portal session (invoice and credit note uploads). */
export async function vendorPortalUpstream(
  path: `/v1/pay/vendor-portal/${string}`,
  init: { method?: string; body?: BodyInit; timeoutMs?: number },
): Promise<Response | null> {
  const token = await readVendorPortalToken();
  if (!token) return null;
  const request = new Headers({ Accept: "application/json", [VENDOR_PORTAL_HEADER]: token, ...(await forwardedIp()) });
  return fetch(`${serverEnv().apiInternalUrl}${path}`, {
    method: init.method ?? "GET",
    headers: request,
    body: init.body ?? null,
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(init.timeoutMs ?? VENDOR_PORTAL_TIMEOUT_MS),
  });
}

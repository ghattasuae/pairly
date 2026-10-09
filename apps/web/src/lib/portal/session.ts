// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The candidate portal's session (WP2.9), on the server only.
 *
 * Candidates are not staff: they never get a Better Auth session. A magic link from their
 * email opens a portal session at the API (`POST /v1/hire/portal/session`); its token
 * lives in an HTTP-only cookie of its own (`__Host-pairly_portal` over HTTPS, so it is
 * bound to this host and path `/`; `pairly_portal` in local development), SameSite Lax,
 * never readable by scripts. Only the portal code reads it, and only to send it to the
 * API in the `X-Pairly-Portal-Session` header. The API never accepts it as a staff
 * credential, and the staff token never reaches portal routes.
 */
import "server-only";

import { cookies, headers } from "next/headers";

import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import { serverEnv } from "../env";

export const PORTAL_HEADER = "X-Pairly-Portal-Session";
export const PORTAL_TIMEOUT_MS = 5000;
const TOKEN_RE = /^ps1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;

function secure(): boolean {
  return serverEnv().baseUrl.startsWith("https://");
}

export function portalCookieName(): string {
  return secure() ? "__Host-pairly_portal" : "pairly_portal";
}

export async function readPortalToken(): Promise<string | null> {
  const value = (await cookies()).get(portalCookieName())?.value ?? null;
  return value && TOKEN_RE.test(value) ? value : null;
}

export async function setPortalToken(token: string, expiresAt: string): Promise<void> {
  if (!TOKEN_RE.test(token)) throw new Error("invalid portal token");
  const expires = new Date(expiresAt);
  (await cookies()).set(portalCookieName(), token, {
    httpOnly: true,
    secure: secure(),
    sameSite: "lax",
    path: "/",
    expires: Number.isNaN(expires.getTime()) ? undefined : expires,
  });
}

export async function clearPortalToken(): Promise<void> {
  (await cookies()).delete(portalCookieName());
}

export type PortalResult<T> = { ok: true; data: T; status: number } | { ok: false; code: string; status: number | null };

async function forwardedIp(): Promise<Record<string, string>> {
  const ip = clientIpFrom(await headers());
  return ip ? { [CLIENT_IP_HEADER]: ip } : {};
}

/**
 * One call to the portal API. `session: true` sends the candidate's portal session (and
 * answers `not_signed_in` without one). Never follows redirects; short timeout.
 */
export async function portalCall<T>(
  path: `/v1/hire/portal/${string}`,
  init: { method?: string; body?: unknown; session?: boolean; okStatuses?: number[] } = {},
): Promise<PortalResult<T>> {
  const { method = "GET", body, session = true, okStatuses = [200, 201, 202, 204] } = init;
  const request = new Headers({ Accept: "application/json", ...(await forwardedIp()) });
  if (session) {
    const token = await readPortalToken();
    if (!token) return { ok: false, code: "not_signed_in", status: null };
    request.set(PORTAL_HEADER, token);
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
      signal: AbortSignal.timeout(PORTAL_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, code: "unavailable", status: null };
  }
  if (!okStatuses.includes(response.status)) {
    let code = response.status === 401 ? "not_signed_in" : response.status === 404 ? "not_found" : "unavailable";
    if (response.status === 409) code = "conflict";
    if (response.status === 429) code = "rate_limited";
    if (response.status === 400 || response.status === 422) code = "invalid_input";
    if (response.status === 413) code = "too_large";
    return { ok: false, code, status: response.status };
  }
  if (response.status === 204) return { ok: true, data: undefined as T, status: 204 };
  try {
    return { ok: true, data: (await response.json()) as T, status: response.status };
  } catch {
    return { ok: false, code: "unavailable", status: response.status };
  }
}

/** Raw upstream request with the portal session (downloads and uploads). */
export async function portalUpstream(
  path: `/v1/hire/portal/${string}`,
  init: { method?: string; body?: BodyInit; accept?: string; timeoutMs?: number; contentType?: string },
): Promise<Response | null> {
  const token = await readPortalToken();
  if (!token) return null;
  const request = new Headers({ Accept: init.accept ?? "application/json", [PORTAL_HEADER]: token, ...(await forwardedIp()) });
  if (init.contentType) request.set("Content-Type", init.contentType);
  return fetch(`${serverEnv().apiInternalUrl}${path}`, {
    method: init.method ?? "GET",
    headers: request,
    body: init.body ?? null,
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(init.timeoutMs ?? PORTAL_TIMEOUT_MS),
  });
}

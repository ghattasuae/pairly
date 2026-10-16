// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Helpers for the few route handlers that pass an API response through to the browser
 * as it is (an event stream, a PDF, an audit export). The token is minted here and stays
 * on the server; the browser only ever talks to this origin.
 */
import "server-only";

import { getApiToken } from "../auth/server";
import { serverEnv } from "../env";
import { buildApiHeaders, getActiveOrgId, NotSignedInError } from "./server";

/**
 * Whether a request comes from one of our own pages. Browsers send `Sec-Fetch-Site`; a
 * cross-site page (a link or form elsewhere) gets `cross-site` or `same-site` and is
 * refused, so another site cannot make a signed-in browser start an export. A request
 * without the header (very old browsers, scripts) is allowed: it carries no cookie
 * unless the person sent it themselves.
 */
export function isSameOriginRequest(request: Request): boolean {
  const site = request.headers.get("sec-fetch-site");
  return site === null || site === "same-origin" || site === "none";
}

export function jsonProblem(status: number, code: string): Response {
  return Response.json({ code }, { status, headers: { "Cache-Control": "no-store" } });
}

/**
 * Open an API request as the signed-in user in their active organisation. A GET unless
 * `json` is given, which is sent as the body of a POST.
 */
export async function upstream(
  path: `/v1/${string}`,
  init: { accept: string; signal?: AbortSignal; extraHeaders?: Record<string, string>; json?: unknown },
): Promise<Response> {
  const token = await getApiToken();
  if (!token) throw new NotSignedInError();
  const headers = buildApiHeaders(token, await getActiveOrgId());
  headers.set("Accept", init.accept);
  for (const [key, value] of Object.entries(init.extraHeaders ?? {})) headers.set(key, value);
  const post = init.json !== undefined;
  if (post) headers.set("Content-Type", "application/json");
  return fetch(`${serverEnv().apiInternalUrl}${path}`, {
    method: post ? "POST" : "GET",
    headers,
    ...(post ? { body: JSON.stringify(init.json) } : {}),
    cache: "no-store",
    redirect: "error",
    ...(init.signal ? { signal: init.signal } : {}),
  });
}

/** Uploads may take a while (virus scan, parse); everything else keeps the short timeout. */
export const UPLOAD_TIMEOUT_MS = 120_000;

/** POST a multipart body to the API as the signed-in user in their active organisation. */
export async function upstreamUpload(
  path: `/v1/${string}`,
  body: FormData,
  init: { signal?: AbortSignal; headers?: Record<string, string> } = {},
): Promise<Response> {
  const token = await getApiToken();
  if (!token) throw new NotSignedInError();
  const headers = buildApiHeaders(token, await getActiveOrgId());
  for (const [key, value] of Object.entries(init.headers ?? {})) headers.set(key, value);
  const timeout = AbortSignal.timeout(UPLOAD_TIMEOUT_MS);
  return fetch(`${serverEnv().apiInternalUrl}${path}`, {
    method: "POST",
    headers,
    body,
    cache: "no-store",
    redirect: "error",
    signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
  });
}

/** The problem `code` of an error response, or a code for its status. */
export async function upstreamProblem(response: Response): Promise<Response> {
  let code = "unavailable";
  try {
    const body = (await response.json()) as { code?: unknown };
    if (typeof body.code === "string" && /^[a-z_]{1,64}$/.test(body.code)) code = body.code;
  } catch {
    if (response.status === 404) code = "not_found";
    else if (response.status === 403) code = "permission_denied";
  }
  const status = [400, 401, 403, 404, 409, 410, 413, 415, 422, 429].includes(response.status) ? response.status : 502;
  return jsonProblem(status, code);
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * One way to call the API from server actions and server components and turn the answer
 * into a small, serialisable result. The API decides access; this only maps its answer
 * (problem+json `code`, else the HTTP status) to a stable code the UI can translate.
 */
import "server-only";

import { getApiClient, getUserApiClient, NotSignedInError, problemCode, type ApiClient } from "./server";

export type ApiResult<T> = { ok: true; data: T; status: number } | { ok: false; code: string; status: number | null };

interface FetchLike<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** A stable code for a failed response: the problem `code`, else one per status class. */
export function codeForStatus(status: number, error: unknown): string {
  const code = problemCode(error);
  if (code) return code;
  if (status === 401) return "not_signed_in";
  if (status === 403) return "permission_denied";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 410) return "gone";
  if (status === 413) return "too_large";
  if (status === 422 || status === 400) return "invalid_input";
  if (status === 429) return "rate_limited";
  return "unavailable";
}

/**
 * Run one API call with the signed-in user's client. `scope: "user"` uses the client
 * without an organisation header (calls whose organisation comes from elsewhere, such as
 * approval links).
 */
export async function callApi<T>(
  run: (api: ApiClient) => Promise<FetchLike<T>>,
  { scope = "org", okStatuses = [200, 201, 202] }: { scope?: "org" | "user"; okStatuses?: number[] } = {},
): Promise<ApiResult<T>> {
  try {
    const api = scope === "user" ? await getUserApiClient() : await getApiClient();
    const { data, error, response } = await run(api);
    if (okStatuses.includes(response.status)) return { ok: true, data: data as T, status: response.status };
    return { ok: false, code: codeForStatus(response.status, error), status: response.status };
  } catch (err) {
    if (err instanceof NotSignedInError) return { ok: false, code: "not_signed_in", status: null };
    return { ok: false, code: "unavailable", status: null };
  }
}

/** Drop `status` so the result can go to the browser as an action result. */
export function toAction<T, U extends object>(
  result: ApiResult<T>,
  map: (data: T) => U,
): ({ ok: true } & U) | { ok: false; code: string } {
  return result.ok ? { ok: true, ...map(result.data) } : { ok: false, code: result.code };
}

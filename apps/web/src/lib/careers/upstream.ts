// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The apply route handlers' calls to the public careers API. No token (the routes are
 * public); the visitor's address goes along for the API's per-IP limits. Answers are
 * passed back as `{ ...data }` or `{ code }` with `Cache-Control: no-store`.
 */
import "server-only";

import { jsonProblem, upstreamProblem } from "../api/bff";
import { serverEnv } from "../env";
import { CLIENT_IP_HEADER, clientIpFrom } from "./format";

export const APPLY_TIMEOUT_MS = 10_000;
export const FILE_TIMEOUT_MS = 120_000;

export async function publicPost(
  request: Request,
  path: `/v1/hire/public/careers/${string}`,
  body: BodyInit,
  init: { json: boolean; timeoutMs: number },
): Promise<Response> {
  const headers = new Headers({ Accept: "application/json" });
  if (init.json) headers.set("Content-Type", "application/json");
  const ip = clientIpFrom(request.headers);
  if (ip) headers.set(CLIENT_IP_HEADER, ip);
  let response: Response;
  try {
    response = await fetch(`${serverEnv().apiInternalUrl}${path}`, {
      method: "POST",
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(init.timeoutMs)]),
    });
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok) return upstreamProblem(response);
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    return jsonProblem(502, "unavailable");
  }
  return Response.json(data, { status: response.status, headers: { "Cache-Control": "no-store" } });
}

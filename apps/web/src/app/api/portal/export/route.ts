// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * "Download my data" in the candidate portal's privacy centre: the candidate's own data as
 * one JSON file (`GET /v1/hire/portal/privacy/export`), with their portal session from its
 * HTTP-only cookie. Same origin only, never cached.
 */
import { isSameOriginRequest, jsonProblem, upstreamProblem } from "@/lib/api/bff";
import { portalUpstream } from "@/lib/portal/session";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  let response: Response | null;
  try {
    response = await portalUpstream("/v1/hire/portal/privacy/export", { timeoutMs: 20_000 });
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (response === null) return jsonProblem(401, "not_signed_in");
  if (!response.ok) return upstreamProblem(response);
  return new Response(response.body, {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Disposition": 'attachment; filename="my-application-data.json"',
      "Cache-Control": "no-store",
    },
  });
}

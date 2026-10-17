// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The bias audit pack (`GET /v1/hire/bias/export`), a zip passed through from the API with
 * the signed-in user's token. The API checks `hire.bias.export`, requires a two-factor
 * sign-in and audits every export; only the validated filters are forwarded.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";
import { biasExportQuery } from "@/lib/hire/bias";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const query = biasExportQuery(new URL(request.url).searchParams);
  let response: Response;
  try {
    response = await upstream(`/v1/hire/bias/export${query}`, { accept: "application/zip", signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": 'attachment; filename="pairly-bias-audit-pack.zip"',
      "Cache-Control": "private, no-store",
    },
  });
}

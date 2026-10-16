// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The month-end report as CSV (`GET /v1/pay/month-end/export?period=YYYY-MM`), passed
 * through. The API checks `pay.month_end.read` and audits the download with the file's
 * digest. Only our own pages may start it: the month-end page sends the
 * `X-Pairly-Export` header, which a link or form on another site cannot.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const PERIOD_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

export async function POST(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request) || request.headers.get("x-pairly-export") !== "1") {
    return jsonProblem(403, "cross_site");
  }
  const period = new URL(request.url).searchParams.get("period") ?? "";
  if (!PERIOD_RE.test(period)) return jsonProblem(400, "invalid_period");
  let response: Response;
  try {
    response = await upstream(`/v1/pay/month-end/export?period=${period}`, { accept: "text/csv", signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="month-end-${period}.csv"`,
      "Cache-Control": "private, no-store",
    },
  });
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The generic journal of a period as CSV (`POST /v1/pay/journal/export`), passed
 * through. The API checks `pay.journal.export` and the TOTP step-up, records the
 * download with the file's SHA-256 and audits it. Only our own pages may start it: the
 * month-end page sends the `X-Pairly-Export` header, which a link or form on another
 * site cannot.
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
    response = await upstream("/v1/pay/journal/export", { accept: "text/csv", json: { period }, signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="journal-${period}.csv"`,
      "Cache-Control": "private, no-store",
    },
  });
}

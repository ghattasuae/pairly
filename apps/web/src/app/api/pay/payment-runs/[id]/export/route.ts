// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The bank payment file of a payment run (`POST /v1/pay/payment-runs/{id}/export`),
 * passed through as CSV or pain.001 XML. The API checks `pay.payment_run.export` and the
 * TOTP step-up, checks every payment again and refuses the file (409, with the problems)
 * when anything changed since the approval. Nothing is sent to a bank. Only our own
 * pages may start it: the run page sends the `X-Pairly-Export` header, which a link or
 * form on another site cannot.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PROBLEM_RE = /^[A-Za-z0-9_:.-]{1,120}$/;

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  if (!isSameOriginRequest(request) || request.headers.get("x-pairly-export") !== "1") {
    return jsonProblem(403, "cross_site");
  }
  const { id } = await params;
  if (!UUID_RE.test(id)) return jsonProblem(404, "not_found");
  const format = new URL(request.url).searchParams.get("format") === "pain001" ? "pain001" : "csv";
  let response: Response;
  try {
    response = await upstream(`/v1/pay/payment-runs/${id}/export`, {
      accept: format === "csv" ? "text/csv" : "application/xml",
      json: { format },
      signal: request.signal,
    });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (response.status === 409) {
    // The refusal names what changed: pass the problem list through, nothing else.
    try {
      const body = (await response.json()) as { code?: unknown; problems?: unknown };
      const problems = Array.isArray(body.problems)
        ? body.problems.filter((p): p is string => typeof p === "string" && PROBLEM_RE.test(p)).slice(0, 50)
        : [];
      const code = typeof body.code === "string" && /^[a-z_]{1,64}$/.test(body.code) ? body.code : "conflict";
      return Response.json({ code, problems }, { status: 409, headers: { "Cache-Control": "no-store" } });
    } catch {
      return jsonProblem(409, "conflict");
    }
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  const disposition = response.headers.get("content-disposition") ?? "";
  const name = /filename="([A-Za-z0-9-]{1,40}\.(csv|xml))"/.exec(disposition)?.[1] ?? `payment-run.${format === "csv" ? "csv" : "xml"}`;
  return new Response(response.body, {
    headers: {
      "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/xml",
      "Content-Disposition": `attachment; filename="${name}"`,
      "Cache-Control": "private, no-store",
    },
  });
}

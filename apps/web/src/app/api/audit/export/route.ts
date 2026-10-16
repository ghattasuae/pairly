// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Audit log export (`GET /v1/audit/export`), streamed through from the API as CSV or
 * JSON with the viewer's filters. The API checks `core.audit.export` and the TOTP step-up
 * and records the export in the audit log. Only our own pages may start one: the viewer
 * fetches it with the `X-Pairly-Export` header, which a link or form on another site
 * cannot send.
 */
import { auditApiQuery, parseAuditFilters } from "@/lib/audit-filters";
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request) || request.headers.get("x-pairly-export") !== "1") {
    return jsonProblem(403, "cross_site");
  }
  const url = new URL(request.url);
  const format = url.searchParams.get("format") === "csv" ? "csv" : "json";
  const query = new URLSearchParams({ format, ...auditApiQuery(parseAuditFilters(url.searchParams)) });
  let response: Response;
  try {
    response = await upstream(`/v1/audit/export?${query.toString()}`, {
      accept: format === "csv" ? "text/csv" : "application/json",
      signal: request.signal,
    });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  const stamp = new Date().toISOString().slice(0, 10);
  return new Response(response.body, {
    headers: {
      "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/json",
      "Content-Disposition": `attachment; filename="audit-${stamp}.${format}"`,
      "Cache-Control": "private, no-store",
    },
  });
}

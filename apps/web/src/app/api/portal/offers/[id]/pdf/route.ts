// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The candidate's offer letter as a PDF (`GET /v1/hire/portal/offers/{id}/pdf`), with
 * their portal session from its HTTP-only cookie. Same origin only, never cached.
 */
import { isSameOriginRequest, jsonProblem, upstreamProblem } from "@/lib/api/bff";
import { portalUpstream } from "@/lib/portal/session";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const { id } = await params;
  if (!UUID_RE.test(id)) return jsonProblem(404, "not_found");
  let response: Response | null;
  try {
    response = await portalUpstream(`/v1/hire/portal/offers/${id}/pdf`, { accept: "application/pdf", timeoutMs: 20_000 });
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (response === null) return jsonProblem(401, "not_signed_in");
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    status: 200,
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": 'attachment; filename="offer.pdf"',
      "Cache-Control": "no-store",
    },
  });
}

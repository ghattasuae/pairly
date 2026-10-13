// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * An offer letter as a PDF (`GET /v1/hire/offers/{id}/pdf?locale=en|ar`), passed through
 * from the API with the signed-in user's token. The API checks the compensation
 * permission (the letter shows the compensation).
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const { id } = await params;
  if (!UUID_RE.test(id)) return jsonProblem(404, "not_found");
  const locale = new URL(request.url).searchParams.get("locale") === "ar" ? "ar" : "en";
  let response: Response;
  try {
    response = await upstream(`/v1/hire/offers/${id}/pdf?locale=${locale}`, { accept: "application/pdf", signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": `attachment; filename="offer-${locale}.pdf"`,
      "Cache-Control": "private, no-store",
    },
  });
}

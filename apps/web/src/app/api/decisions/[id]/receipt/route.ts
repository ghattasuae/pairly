// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * A decision receipt as a PDF (`GET /v1/decisions/{id}/receipt.pdf?lang=en|ar`), passed
 * through from the API with the signed-in user's token. The API redacts by role.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";
import { isDecisionId } from "@/lib/decision-id";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const { id } = await params;
  const lang = new URL(request.url).searchParams.get("lang") === "ar" ? "ar" : "en";
  if (!isDecisionId(id)) return jsonProblem(404, "not_found");
  let response: Response;
  try {
    response = await upstream(`/v1/decisions/${id}/receipt.pdf?lang=${lang}`, {
      accept: "application/pdf",
      signal: request.signal,
    });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": `attachment; filename="receipt-${id}-${lang}.pdf"`,
      "Cache-Control": "private, no-store",
    },
  });
}

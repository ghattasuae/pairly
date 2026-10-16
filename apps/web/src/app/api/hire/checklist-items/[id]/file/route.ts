// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * A pre-employment checklist file (`GET /v1/hire/checklist-items/{id}/file`), passed
 * through from the API with the signed-in user's token. The API allows HR and admins
 * only, decrypts the file and audits the opening. Never cached.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TYPES = new Set(["application/pdf", "image/jpeg", "image/png"]);

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const { id } = await params;
  if (!UUID_RE.test(id)) return jsonProblem(404, "not_found");
  let response: Response;
  try {
    response = await upstream(`/v1/hire/checklist-items/${id}/file`, { accept: "*/*", signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  const type = response.headers.get("content-type") ?? "";
  return new Response(response.body, {
    headers: {
      "Content-Type": TYPES.has(type) ? type : "application/octet-stream",
      "Content-Disposition": response.headers.get("content-disposition") ?? "attachment",
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

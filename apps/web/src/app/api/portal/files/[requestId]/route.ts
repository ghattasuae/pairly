// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * A file the hiring team asked a candidate for, uploaded from the candidate portal
 * (`POST /v1/hire/portal/document-requests/{id}/file`): multipart with `file`. Same origin
 * only (`X-Pairly-Upload: 1`), at most 26 MB, with the candidate's portal session from its
 * HTTP-only cookie. The API stores it through the normal intake (type sniffing, size
 * limits, virus scan) and checks that the request is the candidate's own.
 */
import { jsonProblem, upstreamProblem } from "@/lib/api/bff";
import { serverEnv } from "@/lib/env";
import { portalUpstream } from "@/lib/portal/session";
import { checkUploadRequest, MAX_FILE_BYTES, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export async function POST(request: Request, { params }: { params: Promise<{ requestId: string }> }): Promise<Response> {
  const { requestId } = await params;
  if (!UUID_RE.test(requestId)) return jsonProblem(404, "not_found");
  const refusal = checkUploadRequest(request.headers, serverEnv().trustedOrigins);
  if (refusal) return jsonProblem(refusal.status, refusal.code);
  const raw = await readCapped(request.body, MAX_REQUEST_BYTES);
  if (raw === null) return jsonProblem(413, "too_large");
  let form: FormData;
  try {
    form = await new Response(raw as BodyInit, {
      headers: { "content-type": request.headers.get("content-type") ?? "" },
    }).formData();
  } catch {
    return jsonProblem(400, "invalid_input");
  }
  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0 || file.size > MAX_FILE_BYTES) {
    return jsonProblem(400, "invalid_input");
  }
  const body = new FormData();
  body.set("file", file, file.name.slice(0, 200) || "file");
  let response: Response | null;
  try {
    response = await portalUpstream(`/v1/hire/portal/document-requests/${requestId}/file`, {
      method: "POST",
      body,
      timeoutMs: 120_000,
    });
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (response === null) return jsonProblem(401, "not_signed_in");
  if (!response.ok) return upstreamProblem(response);
  return Response.json(await response.json(), { status: 201, headers: { "Cache-Control": "no-store" } });
}

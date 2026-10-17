// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Policy document upload (`POST /v1/policies/documents`), the only route that accepts a
 * large body (26 MB). The API checks `core.policy.manage` and the TOTP step-up, scans and
 * indexes the file. Only our own pages may upload: same origin and the `X-Pairly-Upload`
 * header (see `src/lib/upload-guard.ts`). Answers `{ ok, policy }` or `{ code }`.
 */
import { jsonProblem, upstreamProblem, upstreamUpload } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";
import { serverEnv } from "@/lib/env";
import { parsePolicyUploadForm } from "@/lib/policy-upload";
import { checkUploadRequest, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
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
  const upload = parsePolicyUploadForm(form);
  if (!upload) return jsonProblem(400, "invalid_input");

  const body = new FormData();
  body.set("file", upload.file, upload.file.name.slice(0, 200) || "policy");
  const query = new URLSearchParams({ name: upload.name, title: upload.title });
  let response: Response;
  try {
    response = await upstreamUpload(`/v1/policies/documents?${query.toString()}`, body, { signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok) return upstreamProblem(response);
  let data: { policy_document?: unknown };
  try {
    data = (await response.json()) as { policy_document?: unknown };
  } catch {
    return jsonProblem(502, "unavailable");
  }
  return Response.json({ ok: true, policy: data.policy_document }, { status: 201, headers: { "Cache-Control": "no-store" } });
}

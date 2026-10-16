// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * A document a vendor adds to its onboarding form (trade licence, TRN certificate, bank
 * letter): `POST /v1/pay/vendor-onboarding/me/documents`, multipart with `file`. Same
 * origin only (`X-Pairly-Upload: 1`), size capped, with the vendor's onboarding session
 * from its HTTP-only cookie. The API stores it through the normal intake (type
 * sniffing, size limits, virus scan) and ties it to that one invite.
 */
import { jsonProblem, upstreamProblem } from "@/lib/api/bff";
import { serverEnv } from "@/lib/env";
import { vendorUpstream } from "@/lib/pay/vendor-session";
import { checkUploadRequest, MAX_FILE_BYTES, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";

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
  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0 || file.size > MAX_FILE_BYTES) {
    return jsonProblem(400, "invalid_input");
  }
  const body = new FormData();
  body.set("file", file, file.name.slice(0, 200) || "file");
  let response: Response | null;
  try {
    response = await vendorUpstream("/v1/pay/vendor-onboarding/me/documents", {
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

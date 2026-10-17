// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * An invoice or a credit note sent from the vendor portal (`POST /v1/pay/vendor-portal/invoices`):
 * multipart with `file`, `document_type` and, for a credit note, `credit_for`. Same origin
 * only (`X-Pairly-Upload: 1`), at most 26 MB, with the contact's portal session from its
 * HTTP-only cookie. The API takes it through Pay intake (type sniffing, size limits, virus
 * scan, file hash dedupe) for the contact's own vendor only.
 */
import { jsonProblem, upstreamProblem } from "@/lib/api/bff";
import { serverEnv } from "@/lib/env";
import { checkUploadRequest, MAX_FILE_BYTES, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";
import { vendorPortalUpstream } from "@/lib/vendor-portal/session";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

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
  const kind = form.get("document_type");
  const creditFor = form.get("credit_for");
  if (!(file instanceof File) || file.size === 0 || file.size > MAX_FILE_BYTES) return jsonProblem(400, "invalid_input");
  if (kind !== "invoice" && kind !== "credit_note") return jsonProblem(400, "invalid_input");
  if (kind === "credit_note" && (typeof creditFor !== "string" || !UUID_RE.test(creditFor))) {
    return jsonProblem(400, "invalid_input");
  }
  const body = new FormData();
  body.set("file", file, file.name.slice(0, 200) || "invoice");
  body.set("document_type", kind);
  if (kind === "credit_note" && typeof creditFor === "string") body.set("credit_for", creditFor);
  let response: Response | null;
  try {
    response = await vendorPortalUpstream("/v1/pay/vendor-portal/invoices", { method: "POST", body, timeoutMs: 120_000 });
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (response === null) return jsonProblem(401, "not_signed_in");
  if (!response.ok) return upstreamProblem(response);
  return Response.json(await response.json(), { status: 202, headers: { "Cache-Control": "no-store" } });
}

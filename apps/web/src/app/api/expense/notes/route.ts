// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Submit an expense note with its receipt (`POST /v1/expense/notes`). The API checks
 * `expense.note.submit` and that the submitter is a person, scans, parses and starts the
 * workflow. Only our own pages may submit: same origin and the `X-Pairly-Upload` header
 * (see `src/lib/upload-guard.ts`); at most 26 MB. The browser's `Idempotency-Key` is
 * passed on, so a double click files one note. Answers `{ ok, id }` or `{ code }`.
 */
import { jsonProblem, upstreamProblem, upstreamUpload } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";
import { serverEnv } from "@/lib/env";
import { idempotencyKey, parseExpenseUploadForm } from "@/lib/expense-upload";
import { checkUploadRequest, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  const refusal = checkUploadRequest(request.headers, serverEnv().trustedOrigins);
  if (refusal) return jsonProblem(refusal.status, refusal.code);
  const key = idempotencyKey(request.headers.get("idempotency-key"));
  if (!key) return jsonProblem(400, "invalid_input");

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
  const upload = parseExpenseUploadForm(form);
  if (!upload) return jsonProblem(400, "invalid_input");

  const body = new FormData();
  body.set("file", upload.file, upload.file.name.slice(0, 200) || "receipt");
  if (upload.note) body.set("note", upload.note);
  let response: Response;
  try {
    response = await upstreamUpload("/v1/expense/notes", body, {
      signal: request.signal,
      headers: { "Idempotency-Key": key },
    });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok) return upstreamProblem(response);
  let data: { id?: unknown };
  try {
    data = (await response.json()) as { id?: unknown };
  } catch {
    return jsonProblem(502, "unavailable");
  }
  if (typeof data.id !== "string") return jsonProblem(502, "unavailable");
  return Response.json({ ok: true, id: data.id }, { status: 202, headers: { "Cache-Control": "no-store" } });
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Upload one application file (`POST /v1/hire/public/careers/{org}/applications/files`):
 * multipart with `file`, the upload `token` and the form `field` (`cv` or an optional
 * file). Public, same origin only (`X-Pairly-Upload: 1`), at most 26 MB. The API stores
 * it through the normal intake (type sniffing, size limits, virus scan, parsing).
 */
import { jsonProblem } from "@/lib/api/bff";
import { isUploadToken } from "@/lib/careers/apply";
import { isSlug } from "@/lib/careers/format";
import { FILE_TIMEOUT_MS, publicPost } from "@/lib/careers/upstream";
import { serverEnv } from "@/lib/env";
import { checkUploadRequest, MAX_FILE_BYTES, MAX_REQUEST_BYTES, readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

const FIELD = /^[a-z][a-z0-9_]{0,47}$/;

export async function POST(request: Request, { params }: { params: Promise<{ orgSlug: string }> }): Promise<Response> {
  const { orgSlug } = await params;
  if (!isSlug(orgSlug)) return jsonProblem(404, "not_found");
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
  const token = form.get("token");
  const field = form.get("field") ?? "cv";
  if (
    !(file instanceof File) ||
    file.size === 0 ||
    file.size > MAX_FILE_BYTES ||
    !isUploadToken(token) ||
    typeof field !== "string" ||
    !FIELD.test(field)
  ) {
    return jsonProblem(400, "invalid_input");
  }
  const body = new FormData();
  body.set("token", token);
  body.set("field", field);
  body.set("file", file, file.name.slice(0, 200) || "file");
  return publicPost(request, `/v1/hire/public/careers/${orgSlug}/applications/files`, body, {
    json: false,
    timeoutMs: FILE_TIMEOUT_MS,
  });
}

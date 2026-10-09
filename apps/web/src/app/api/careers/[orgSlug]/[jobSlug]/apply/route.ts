// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Start an application (`POST /v1/hire/public/careers/{org}/jobs/{job}/applications`):
 * consent, contact details, answers, optional self-identification and the human check.
 * Public, same origin only (`X-Pairly-Apply: 1`), at most 64 KB. Answers the API's
 * `{ token, expires_at, file_fields, required_files }` or `{ code }`.
 */
import { jsonProblem } from "@/lib/api/bff";
import { checkApplyRequest, MAX_JSON_BYTES, parseApplyBody } from "@/lib/careers/apply";
import { isSlug } from "@/lib/careers/format";
import { publicPost, APPLY_TIMEOUT_MS } from "@/lib/careers/upstream";
import { serverEnv } from "@/lib/env";
import { readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ orgSlug: string; jobSlug: string }> },
): Promise<Response> {
  const { orgSlug, jobSlug } = await params;
  if (!isSlug(orgSlug) || !isSlug(jobSlug)) return jsonProblem(404, "not_found");
  const refusal = checkApplyRequest(request.headers, serverEnv().trustedOrigins);
  if (refusal) return jsonProblem(refusal.status, refusal.code);
  const raw = await readCapped(request.body, MAX_JSON_BYTES);
  if (raw === null) return jsonProblem(413, "too_large");
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return jsonProblem(400, "invalid_input");
  }
  const body = parseApplyBody(parsed);
  if (!body) return jsonProblem(400, "invalid_input");
  return publicPost(request, `/v1/hire/public/careers/${orgSlug}/jobs/${jobSlug}/applications`, JSON.stringify(body), {
    json: true,
    timeoutMs: APPLY_TIMEOUT_MS,
  });
}

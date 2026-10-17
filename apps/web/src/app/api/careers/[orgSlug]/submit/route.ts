// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Submit an application (`POST /v1/hire/public/careers/{org}/applications/submit`) once
 * its CV is uploaded. Public, same origin only (`X-Pairly-Apply: 1`). Answers
 * `{ status, reference, response_days }` or `{ code }`.
 */
import { jsonProblem } from "@/lib/api/bff";
import { checkApplyRequest, isUploadToken } from "@/lib/careers/apply";
import { isSlug } from "@/lib/careers/format";
import { APPLY_TIMEOUT_MS, publicPost } from "@/lib/careers/upstream";
import { serverEnv } from "@/lib/env";
import { readCapped } from "@/lib/upload-guard";

export const dynamic = "force-dynamic";

export async function POST(request: Request, { params }: { params: Promise<{ orgSlug: string }> }): Promise<Response> {
  const { orgSlug } = await params;
  if (!isSlug(orgSlug)) return jsonProblem(404, "not_found");
  const refusal = checkApplyRequest(request.headers, serverEnv().trustedOrigins);
  if (refusal) return jsonProblem(refusal.status, refusal.code);
  const raw = await readCapped(request.body, 1024);
  if (raw === null) return jsonProblem(413, "too_large");
  let token: unknown;
  try {
    token = (JSON.parse(new TextDecoder().decode(raw)) as { token?: unknown }).token;
  } catch {
    return jsonProblem(400, "invalid_input");
  }
  if (!isUploadToken(token)) return jsonProblem(400, "invalid_input");
  return publicPost(request, `/v1/hire/public/careers/${orgSlug}/applications/submit`, JSON.stringify({ token }), {
    json: true,
    timeoutMs: APPLY_TIMEOUT_MS,
  });
}

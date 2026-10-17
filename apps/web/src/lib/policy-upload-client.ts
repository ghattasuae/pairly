// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The browser side of the policy document upload: posts the form to our own route
 * handler with the custom header it requires, and returns an action-style result.
 */
import type { ActionResult } from "./action-result";
import type { PolicyDocument } from "./api/types";
import { UPLOAD_ROUTE } from "./policy-upload";
import { UPLOAD_HEADER } from "./upload-guard";

const CODE = /^[a-z_]{1,64}$/;

export async function uploadPolicyDocument(form: FormData): Promise<ActionResult<{ policy: PolicyDocument }>> {
  let response: Response;
  try {
    response = await fetch(UPLOAD_ROUTE, {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: { [UPLOAD_HEADER]: "1" },
    });
  } catch {
    return { ok: false, code: "unavailable" };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON (a proxy error page): reported by status below.
  }
  const data = (typeof body === "object" && body !== null ? body : {}) as { ok?: unknown; policy?: unknown; code?: unknown };
  if (response.ok && data.ok === true && typeof data.policy === "object" && data.policy !== null) {
    return { ok: true, policy: data.policy as PolicyDocument };
  }
  if (typeof data.code === "string" && CODE.test(data.code)) return { ok: false, code: data.code };
  if (response.status === 413) return { ok: false, code: "too_large" };
  return { ok: false, code: "unavailable" };
}

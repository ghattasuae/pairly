// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The browser side of submitting an expense note: posts the form to our own route
 * handler with the custom header it requires and one idempotency key per submission.
 */
import type { ActionResult } from "./action-result";
import { EXPENSE_UPLOAD_ROUTE } from "./expense-upload";
import { UPLOAD_HEADER } from "./upload-guard";

const CODE = /^[a-z_]{1,64}$/;

export async function submitExpenseNote(form: FormData, key: string): Promise<ActionResult<{ id: string }>> {
  let response: Response;
  try {
    response = await fetch(EXPENSE_UPLOAD_ROUTE, {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: { [UPLOAD_HEADER]: "1", "Idempotency-Key": key },
    });
  } catch {
    return { ok: false, code: "unavailable" };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON: reported by status below.
  }
  const data = (typeof body === "object" && body !== null ? body : {}) as { ok?: unknown; id?: unknown; code?: unknown };
  if (response.ok && data.ok === true && typeof data.id === "string") return { ok: true, id: data.id };
  if (typeof data.code === "string" && CODE.test(data.code)) return { ok: false, code: data.code };
  if (response.status === 413) return { ok: false, code: "too_large" };
  return { ok: false, code: "unavailable" };
}

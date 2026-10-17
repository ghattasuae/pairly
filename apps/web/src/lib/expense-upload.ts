// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Expense receipt upload, shared by the route handler (`/api/expense/notes`) and the
 * browser. Pure, no I/O. The request checks live in `upload-guard.ts`.
 */
import { MAX_FILE_BYTES } from "./upload-guard";

export const EXPENSE_UPLOAD_ROUTE = "/api/expense/notes";
/** The API's limit for the note that goes with a receipt. */
export const MAX_NOTE_LENGTH = 1000;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9_-]{8,128}$/;

export interface ExpenseUpload {
  file: File;
  note: string;
}

/** The checked fields of the receipt form, or null when anything is off. */
export function parseExpenseUploadForm(form: FormData): ExpenseUpload | null {
  const file = form.get("file");
  const note = form.get("note") ?? "";
  if (
    !(file instanceof File) ||
    file.size === 0 ||
    file.size > MAX_FILE_BYTES ||
    typeof note !== "string" ||
    note.length > MAX_NOTE_LENGTH
  ) {
    return null;
  }
  return { file, note: note.trim() };
}

/** The browser's idempotency key for one submission, if it is well formed. */
export function idempotencyKey(value: string | null): string | null {
  return value !== null && IDEMPOTENCY_KEY.test(value) ? value : null;
}

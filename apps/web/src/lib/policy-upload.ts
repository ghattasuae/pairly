// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Policy document upload, shared by the route handler (`/api/policies/documents`) and the
 * browser. Pure, no I/O. The request checks live in `upload-guard.ts`.
 */
import { z } from "zod";

import { MAX_FILE_BYTES } from "./upload-guard";

export const UPLOAD_ROUTE = "/api/policies/documents";

const policyName = z.string().regex(/^[a-z][a-z0-9_.-]{0,127}$/);

export interface PolicyUpload {
  name: string;
  title: string;
  file: File;
}

/** The checked fields of the upload form, or null when anything is off. */
export function parsePolicyUploadForm(form: FormData): PolicyUpload | null {
  const name = form.get("name");
  const title = form.get("title");
  const file = form.get("file");
  if (
    !policyName.safeParse(name).success ||
    typeof title !== "string" ||
    title.trim().length < 1 ||
    title.length > 200 ||
    !(file instanceof File) ||
    file.size === 0 ||
    file.size > MAX_FILE_BYTES
  ) {
    return null;
  }
  return { name: name as string, title: title.trim(), file };
}

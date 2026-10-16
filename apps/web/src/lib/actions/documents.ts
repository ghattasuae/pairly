// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * The document viewer's source: a short-lived signed URL for the document's bytes
 * (`GET /v1/documents/{id}/content-url`, which the API audits). Asked for just before
 * rendering, because the URL expires within a minute. The API checks the workflow's
 * document read permission and refuses files that have not passed the virus scan.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";

export async function documentSource(
  documentId: string,
): Promise<ActionResult<{ url: string; mediaType: string; filename: string | null }>> {
  if (!z.uuid().safeParse(documentId).success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.GET("/v1/documents/{document_id}/content-url", { params: { path: { document_id: documentId } } }),
  );
  if (result.ok) {
    // Only plain http(s) URLs ever reach an <img> or the PDF loader.
    const protocol = (() => {
      try {
        return new URL(result.data.url).protocol;
      } catch {
        return "";
      }
    })();
    if (protocol !== "https:" && protocol !== "http:") return { ok: false, code: "unavailable" };
  }
  return toAction(result, (data) => ({ url: data.url, mediaType: data.media_type, filename: data.filename }));
}

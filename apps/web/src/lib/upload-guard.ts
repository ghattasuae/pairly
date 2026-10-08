// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Guards for the route handlers that accept a file upload (policy documents, expense
 * receipts). Pure, no I/O, so it is unit tested.
 *
 * Uploads go through dedicated route handlers, not server actions, so only those routes
 * accept a large body (26 MB: the API's 25 MB file limit plus the multipart envelope)
 * and server actions keep Next's default 1 MB limit.
 *
 * Cross-site requests are refused twice over: the browser must say the request is
 * same-origin (`Sec-Fetch-Site`, else an `Origin` we trust), and it must carry the custom
 * `X-Pairly-Upload` header, which a form or link on another site cannot send (a
 * cross-origin `fetch` with it needs a CORS preflight we never answer).
 */

export const UPLOAD_HEADER = "x-pairly-upload";
/** The API's limit for one uploaded file (policy documents, receipts). */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
/** The route handler's limit for the whole request body (file plus multipart envelope). */
export const MAX_REQUEST_BYTES = 26 * 1024 * 1024;

export type UploadRefusal = { status: number; code: string };

/**
 * Whether a request may upload: same origin, our custom header, a multipart body within
 * the limit (by its declared length; the body is also counted while it is read).
 * `trustedOrigins` are exact origins (the app's own first).
 */
export function checkUploadRequest(
  headers: Pick<Headers, "get">,
  trustedOrigins: readonly string[],
): UploadRefusal | null {
  const site = headers.get("sec-fetch-site");
  const origin = headers.get("origin");
  if (site !== null && site !== "same-origin") return { status: 403, code: "cross_site" };
  if (origin !== null && !trustedOrigins.includes(origin)) return { status: 403, code: "cross_site" };
  // Neither header: not a browser request we can vouch for.
  if (site === null && origin === null) return { status: 403, code: "cross_site" };
  if (headers.get(UPLOAD_HEADER) !== "1") return { status: 403, code: "cross_site" };
  const type = headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("multipart/form-data")) return { status: 415, code: "invalid_input" };
  const length = headers.get("content-length");
  if (length !== null) {
    const declared = Number(length);
    if (!Number.isSafeInteger(declared) || declared < 0) return { status: 400, code: "invalid_input" };
    if (declared > MAX_REQUEST_BYTES) return { status: 413, code: "too_large" };
  }
  return null;
}

/**
 * Read a request body into memory, refusing it as soon as it passes `limit` bytes
 * (a missing or lying `Content-Length` cannot get a bigger body through).
 */
export async function readCapped(body: ReadableStream<Uint8Array> | null, limit: number): Promise<Uint8Array | null> {
  if (body === null) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

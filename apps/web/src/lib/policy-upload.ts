// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Policy document upload, shared by the route handler (`/api/policies/documents`) and the
 * browser. Pure, no I/O, so it is unit tested.
 *
 * Uploads go through a dedicated route handler, not a server action, so only this one
 * route accepts a large body (26 MB: the API's 25 MB file limit plus the multipart
 * envelope) and server actions keep Next's default 1 MB limit.
 *
 * Cross-site requests are refused twice over: the browser must say the request is
 * same-origin (`Sec-Fetch-Site`, else an `Origin` we trust), and it must carry the custom
 * `X-Pairly-Upload` header, which a form or link on another site cannot send (a
 * cross-origin `fetch` with it needs a CORS preflight we never answer).
 */
import { z } from "zod";

export const UPLOAD_HEADER = "x-pairly-upload";
export const UPLOAD_ROUTE = "/api/policies/documents";
/** The API's limit for one policy document. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
/** The route handler's limit for the whole request body (file plus multipart envelope). */
export const MAX_REQUEST_BYTES = 26 * 1024 * 1024;

const policyName = z.string().regex(/^[a-z][a-z0-9_.-]{0,127}$/);

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

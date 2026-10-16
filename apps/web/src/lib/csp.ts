// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Content Security Policy for every page.
 *
 * Scripts: only same-origin scripts carrying this request's nonce ('strict-dynamic' lets
 * them load their own chunks). No inline event handlers, no eval outside development.
 * Workers: same origin only (the PDF viewer's worker is served from /pdfjs/).
 * Styles: 'unsafe-inline' is allowed because React and Radix set style attributes, which a
 * nonce cannot cover. Style injection cannot run code, so the trade is accepted.
 * Connections: same origin, plus the document store origin (`documentOrigin`) so the
 * document viewer can fetch a document through its short-lived signed URL. The browser
 * never talks to the API directly.
 */
export function buildCsp({
  nonce,
  dev,
  https,
  documentOrigin = null,
}: {
  nonce: string;
  dev: boolean;
  https: boolean;
  /** Exact origin of signed document URLs (object storage), or null. */
  documentOrigin?: string | null;
}): string {
  const docs = documentOrigin ? ` ${documentOrigin}` : "";
  const directives: string[] = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "worker-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' blob: data:${docs}`,
    "font-src 'self'",
    `connect-src 'self'${docs}${dev ? " ws: wss:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  if (https) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

/**
 * The exact origin of signed document URLs from `PAIRLY_DOCUMENT_ORIGIN`, or null.
 *
 * Only `scheme://host[:port]` passes: no wildcard, path, query or credentials, so a typo
 * can never widen the policy. In development it defaults to the compose MinIO port.
 */
export function documentOriginFrom(raw: string | undefined, dev: boolean, minioPort?: string): string | null {
  const candidate = raw?.trim() || (dev ? `http://localhost:${minioPort?.trim() || "9000"}` : "");
  if (!candidate) return null;
  try {
    const parsed = new URL(candidate);
    const exact =
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      !candidate.includes("*") &&
      candidate.replace(/\/$/, "") === parsed.origin;
    return exact ? parsed.origin : null;
  } catch {
    return null;
  }
}

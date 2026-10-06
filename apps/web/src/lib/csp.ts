// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Content Security Policy for every page.
 *
 * Scripts: only same-origin scripts carrying this request's nonce ('strict-dynamic' lets
 * them load their own chunks). No inline event handlers, no eval outside development.
 * Styles: 'unsafe-inline' is allowed because React and Radix set style attributes, which a
 * nonce cannot cover. Style injection cannot run code, so the trade is accepted.
 * Connections: same origin only. The browser never talks to the API directly yet.
 */
export function buildCsp({ nonce, dev, https }: { nonce: string; dev: boolean; https: boolean }): string {
  const directives: string[] = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data:",
    "font-src 'self'",
    `connect-src 'self'${dev ? " ws: wss:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  if (https) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

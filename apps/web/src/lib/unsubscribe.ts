// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** What an unsubscribe link does, as the API reports it (`GET /v1/notifications/unsubscribe`). */
export type UnsubscribeView =
  | { kind: "valid"; category: string }
  | { kind: "expired" }
  | { kind: "invalid" }
  | { kind: "unavailable" };

/** `v1.<org hex>.<user hex>.<category>.<issued>.<mac>`: the shape only, never trusted. */
const TOKEN_RE = /^v1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[a-z][a-z0-9_]{0,31}\.[0-9]{1,12}\.[A-Za-z0-9_-]{20,64}$/;

export function isWellFormedUnsubscribeToken(token: string): boolean {
  return token.length <= 200 && TOKEN_RE.test(token);
}

/** The token from a URL fragment (`#token=...`), or null. */
export function readUnsubscribeToken(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const token = params.get("token");
  return token && isWellFormedUnsubscribeToken(token) ? token : null;
}

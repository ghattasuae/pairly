// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Approval link tokens. Emails and Slack buttons point at `/approvals/link#<token>`: the
 * token is in the fragment, which browsers never send to a server, so it stays out of
 * logs and Referer headers. The page keeps it in this browser for at most 30 minutes
 * (the link's own lifetime) so the person can sign in and come back, and forgets it as
 * soon as it is used or found invalid.
 */

const TOKEN = /^[A-Za-z0-9_-]{43}$/;
export const PENDING_LINK_KEY = "pairly.pendingApprovalLink";
export const PENDING_LINK_MS = 30 * 60 * 1000;

/** The token from a fragment: `#<token>` (as the API builds it) or `#token=<token>`. */
export function readLinkToken(hash: string): string | null {
  const raw = hash.replace(/^#/, "");
  if (TOKEN.test(raw)) return raw;
  const named = new URLSearchParams(raw).get("token");
  return named && TOKEN.test(named) ? named : null;
}

export function saveLinkToken(token: string, now = Date.now()): void {
  try {
    localStorage.setItem(PENDING_LINK_KEY, JSON.stringify({ token, at: now }));
  } catch {
    /* storage blocked: the fragment still works in this tab */
  }
}

export function loadLinkToken(now = Date.now()): string | null {
  try {
    const raw = localStorage.getItem(PENDING_LINK_KEY);
    const parsed = raw ? (JSON.parse(raw) as { token?: unknown; at?: unknown }) : null;
    if (!parsed || typeof parsed.token !== "string" || typeof parsed.at !== "number") return null;
    if (now - parsed.at > PENDING_LINK_MS || now < parsed.at) return null;
    return TOKEN.test(parsed.token) ? parsed.token : null;
  } catch {
    return null;
  }
}

export function forgetLinkToken(): void {
  try {
    localStorage.removeItem(PENDING_LINK_KEY);
  } catch {
    /* nothing to forget */
  }
}

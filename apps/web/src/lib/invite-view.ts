// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

export interface InvitePreview {
  org_name: string;
  email: string;
  role: string;
  workflow: string | null;
  expires_at: string;
}

/** What the invite page shows for a token and the current session. */
export type InviteView =
  | { kind: "invalid" }
  | { kind: "unavailable" }
  /** Not signed in: send a magic link to the invited address, then come back. */
  | { kind: "signIn"; preview: InvitePreview }
  /** Signed in as the invited address. */
  | { kind: "accept"; preview: InvitePreview; currentEmail: string }
  /** Signed in as somebody else. */
  | { kind: "wrongUser"; preview: InvitePreview; currentEmail: string };

export function inviteViewFor(preview: InvitePreview, currentEmail: string | null): InviteView {
  if (!currentEmail) return { kind: "signIn", preview };
  if (currentEmail.toLowerCase() === preview.email.toLowerCase()) return { kind: "accept", preview, currentEmail };
  return { kind: "wrongUser", preview, currentEmail };
}

/**
 * The invite link: `/<locale>/invite#token=...`. The token is in the fragment, so it never
 * reaches a server log or a Referer header (the same pattern as the magic link).
 */
export function inviteLinkPath(locale: string, token: string): string {
  return `/${locale}/invite#${new URLSearchParams({ token }).toString()}`;
}

/** The invite token from a URL fragment, if it is well formed. */
export function readInviteToken(hash: string): string | null {
  const token = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return token && /^[A-Za-z0-9_-]{43}$/.test(token) ? token : null;
}

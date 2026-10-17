// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Invitation helpers for the web server. The API owns invitations (create, list, revoke,
 * accept); this module only looks one up for the invite page, prepares sign-in for the
 * invited address, and builds the link that goes in the email.
 */
import "server-only";

import type { InvitationPreview } from "@pairly/shared-ts";

import type { Locale } from "@/i18n/routing";

import { getPublicApiClient } from "./api/server";
import { getAuth } from "./auth/config";
import { serverEnv } from "./env";
import { isWellFormedInviteToken } from "./invite-token";
import { inviteLinkPath } from "./invite-view";

export type PreviewResult =
  | { status: "valid"; preview: InvitationPreview }
  | { status: "invalid" }
  | { status: "unavailable" };

/** What `token` invites to. Every unusable token is just "invalid". */
export async function previewInvitation(token: string): Promise<PreviewResult> {
  if (!isWellFormedInviteToken(token)) return { status: "invalid" };
  try {
    const { data, response } = await getPublicApiClient().POST("/v1/invitations/preview", {
      body: { token },
    });
    if (response.status === 200 && data) return { status: "valid", preview: data };
    return response.status >= 500 ? { status: "unavailable" } : { status: "invalid" };
  } catch {
    return { status: "unavailable" };
  }
}

/**
 * Make sure Better Auth has a user for the invited address, so the invitee can sign in
 * with a magic link (sign-up is closed). Only ever called with the address of a valid,
 * pending invitation. The user starts unverified; the magic link verifies it.
 */
export async function ensureInvitedUser(email: string): Promise<void> {
  const { internalAdapter } = await getAuth().$context;
  const found = await internalAdapter.findUserByEmail(email);
  if (found?.user) return;
  await internalAdapter.createUser({ email, name: "", emailVerified: false }, { method: "pairly-invitation" });
}

/** The invite page for `token`, with the token in the fragment (lib/invite-view.ts). */
export function inviteUrl(locale: Locale, token: string): string {
  return new URL(inviteLinkPath(locale, token), serverEnv().baseUrl).toString();
}

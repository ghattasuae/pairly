// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for invitations. Each one re-checks its input, calls the API with the
 * signed-in user's token and returns a small result for the UI. The API decides access
 * (core.users.manage, TOTP step-up, who may grant which role); nothing here does.
 */
import { z } from "zod";

import { locales } from "@/i18n/routing";

import { getApiClient, getUserApiClient, NotSignedInError, problemCode } from "../api/server";
import { getSession } from "../auth/server";
import { sendInvitationEmail } from "../email";
import { ensureInvitedUser, inviteUrl, previewInvitation } from "../invitations";
import { isWellFormedInviteToken } from "../invite-token";
import { activateOrganization } from "../org-mirror";
import { resolveViewer } from "../provisioning";

export type ActionResult<T extends object = object> = ({ ok: true } & T) | { ok: false; code: string };

const fail = (code: string): { ok: false; code: string } => ({ ok: false, code });

const inviteSchema = z.object({
  email: z.email().max(254),
  role: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
  workflow: z.enum(["hire", "pay"]).nullable(),
  locale: z.enum(locales),
});

export type InviteInput = z.infer<typeof inviteSchema>;

/** Invite someone, then email them the link in the language the inviter picked. */
export async function inviteMember(
  input: InviteInput,
): Promise<ActionResult<{ emailed: boolean; link?: string }>> {
  const parsed = inviteSchema.safeParse(input);
  if (!parsed.success) return fail("invalid_input");
  const { email, role, workflow, locale } = parsed.data;
  try {
    const viewer = await resolveViewer();
    if (viewer.status !== "ready") return fail("forbidden");
    const client = await getApiClient();
    const { data, error, response } = await client.POST("/v1/invitations", {
      body: { email, role, workflow },
    });
    if (response.status !== 201 || !data) return fail(problemCode(error) ?? "unavailable");
    const link = inviteUrl(locale, data.token);
    const session = await getSession();
    try {
      await sendInvitationEmail({
        to: data.invitation.email,
        url: link,
        locale,
        orgName: viewer.me.org.name,
        role: data.invitation.role,
        inviter: session?.user.name || session?.user.email || viewer.me.email,
      });
      return { ok: true, emailed: true };
    } catch {
      // The invitation exists. Showing its link to the inviter is safe: only someone
      // signed in with the invited address can accept it.
      return { ok: true, emailed: false, link };
    }
  } catch (err) {
    return fail(err instanceof NotSignedInError ? "not_signed_in" : "unavailable");
  }
}

/** Revoke a pending invitation. */
export async function revokeInvitation(invitationId: string): Promise<ActionResult> {
  if (!z.uuid().safeParse(invitationId).success) return fail("invalid_input");
  try {
    const { error, response } = await (await getApiClient()).DELETE("/v1/invitations/{invitation_id}", {
      params: { path: { invitation_id: invitationId } },
    });
    return response.status === 200 ? { ok: true } : fail(problemCode(error) ?? "unavailable");
  } catch (err) {
    return fail(err instanceof NotSignedInError ? "not_signed_in" : "unavailable");
  }
}

/**
 * Before the invitee signs in: check the token and make sure Better Auth knows the invited
 * address, so the magic link can be sent to it. Returns the address to send it to.
 */
export async function prepareInviteSignIn(token: string): Promise<ActionResult<{ email: string }>> {
  if (!isWellFormedInviteToken(token)) return fail("invitation_invalid");
  const found = await previewInvitation(token);
  if (found.status !== "valid") return fail(found.status === "invalid" ? "invitation_invalid" : "unavailable");
  try {
    await ensureInvitedUser(found.preview.email);
  } catch {
    return fail("unavailable");
  }
  return { ok: true, email: found.preview.email };
}

/** Accept as the signed-in user, then switch the session to the new organisation. */
export async function acceptInvitation(token: string): Promise<ActionResult<{ orgName: string }>> {
  if (!isWellFormedInviteToken(token)) return fail("invitation_invalid");
  const session = await getSession();
  if (!session) return fail("not_signed_in");
  try {
    const { data, error, response } = await (await getUserApiClient()).POST("/v1/invitations/accept", {
      body: { token },
    });
    if (response.status !== 200 || !data) return fail(problemCode(error) ?? "unavailable");
    await activateOrganization({ id: data.org_id, name: data.org_name, slug: data.org_slug }, session.user.id);
    return { ok: true, orgName: data.org_name };
  } catch {
    return fail("unavailable");
  }
}

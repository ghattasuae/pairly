// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

import { z } from "zod";

import { listMyOrgs } from "../api/server";
import { getSession } from "../auth/server";
import { activateOrganization } from "../org-mirror";

/**
 * Switch the active organisation. Only to one the API lists for this user
 * (`GET /v1/me/orgs`); anything else is refused without touching the session.
 */
export async function switchOrganization(orgId: string): Promise<{ ok: boolean }> {
  if (!z.uuid().safeParse(orgId).success) return { ok: false };
  const session = await getSession();
  if (!session) return { ok: false };
  try {
    const org = (await listMyOrgs())?.find((o) => o.id === orgId);
    if (!org) return { ok: false };
    await activateOrganization(org, session.user.id);
    return { ok: true };
  } catch {
    // Not signed in any more, or the API or database did not answer.
    return { ok: false };
  }
}

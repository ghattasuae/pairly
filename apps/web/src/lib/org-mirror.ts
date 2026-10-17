// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The Better Auth organization mirror.
 *
 * The API owns organisations and memberships. Better Auth only needs to know the active
 * organisation of a session (it becomes `X-Pairly-Org`), and its organizations plugin
 * insists that the user is a member of it. So, for an organisation the API has just
 * confirmed for this user (an accepted invitation, or `GET /v1/me/orgs`), this module
 * creates the Better Auth organization with the SAME UUID if it is missing, adds the user
 * as a plain member if missing, and makes it the session's active organization.
 *
 * It writes through Better Auth's own adapter, in-process, so nothing outside the web app
 * touches the `auth` schema and no browser can call it (the plugin's HTTP endpoints are
 * blocked). The mirror grants nothing: every API call is still authorised by the API.
 */
import "server-only";

import type { OrgSummary } from "@pairly/shared-ts";
import { headers } from "next/headers";

import { getAuth } from "./auth/config";

interface MirrorOrg {
  id: string;
  name: string;
  slug: string;
}

export async function ensureOrgMirror(org: OrgSummary, userId: string): Promise<void> {
  const { adapter } = await getAuth().$context;
  const existing = await adapter.findOne<MirrorOrg>({
    model: "organization",
    where: [{ field: "id", value: org.id }],
  });
  if (!existing) {
    await adapter.create({
      model: "organization",
      data: { id: org.id, name: org.name, slug: org.slug, createdAt: new Date() },
      forceAllowId: true,
    });
  } else if (existing.name !== org.name || existing.slug !== org.slug) {
    await adapter.update({
      model: "organization",
      where: [{ field: "id", value: org.id }],
      update: { name: org.name, slug: org.slug },
    });
  }
  const member = await adapter.findOne({
    model: "member",
    where: [
      { field: "organizationId", value: org.id },
      { field: "userId", value: userId },
    ],
  });
  if (!member) {
    await adapter.create({
      model: "member",
      data: { organizationId: org.id, userId, role: "member", createdAt: new Date() },
    });
  }
}

/** Mirror `org` for `userId` and make it the active organization of this session. */
export async function activateOrganization(org: OrgSummary, userId: string): Promise<void> {
  await ensureOrgMirror(org, userId);
  await getAuth().api.setActiveOrganization({ headers: await headers(), body: { organizationId: org.id } });
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { setRequestLocale } from "next-intl/server";
import type * as React from "react";

import { AppShell } from "@/components/shell/app-shell";
import { redirect } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { getSession, listOrganizations } from "@/lib/auth/server";
import { visibleNav } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}

/**
 * Signed-in area. The proxy already bounced requests without a session cookie; this is the
 * real check (cookie present but session invalid or expired).
 */
export default async function AppLayout({ children, params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const session = await getSession();
  if (!session) return redirect({ href: "/sign-in", locale });

  const [viewer, orgs] = await Promise.all([resolveViewer(), listOrganizations()]);
  // A token the API rejects (401) is a configuration fault (issuer, audience, keys), not a
  // signed-out user: redirecting to sign-in would loop. Pages show the unavailable state.
  const permissions = viewer.status === "ready" ? viewer.me.permissions : null;
  const activeOrgId = (session.session as { activeOrganizationId?: string | null }).activeOrganizationId ?? null;

  return (
    <AppShell
      items={visibleNav(permissions)}
      user={{ name: session.user.name || null, email: session.user.email }}
      orgs={orgs.map((org) => ({ id: org.id, name: org.name }))}
      activeOrgId={activeOrgId}
    >
      {children}
    </AppShell>
  );
}

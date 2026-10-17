// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PortalHome } from "@/components/portal/portal-home";
import { PortalSignIn } from "@/components/portal/portal-sign-in";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { portalCall, readPortalToken } from "@/lib/portal/session";
import type { PortalApplication, PortalJob, PortalMe } from "@/lib/portal/steps";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

const ORG = /^[a-z0-9][a-z0-9-]{0,79}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Portal" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/<locale>/portal`: the candidate's applications, or the sign-in form (a magic link by
 * email) when there is no portal session. `?org=<careers slug>` names the organisation for
 * the sign-in form (the careers pages link here with it).
 */
export default async function PortalPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const query = await searchParams;
  const rawOrg = Array.isArray(query.org) ? query.org[0] : query.org;
  const org = rawOrg && ORG.test(rawOrg) ? rawOrg : null;
  const token = await readPortalToken();
  if (token === null) return <PortalSignIn org={org} />;
  const [me, apps, jobs] = await Promise.all([
    portalCall<PortalMe>("/v1/hire/portal/me"),
    portalCall<PortalApplication[]>("/v1/hire/portal/applications"),
    portalCall<PortalJob[]>("/v1/hire/portal/jobs"),
  ]);
  if (!me.ok || !apps.ok) {
    if ((!me.ok && me.code === "not_signed_in") || (!apps.ok && apps.code === "not_signed_in")) {
      // The ended session still names its organisation: offer a new link there.
      return <PortalSignIn org={org ?? token.split(".")[1] ?? null} expired />;
    }
    return <Unavailable retryHref={`/${locale}/portal`} />;
  }
  return <PortalHome me={me.data} applications={apps.data} jobs={jobs.ok ? jobs.data : []} />;
}

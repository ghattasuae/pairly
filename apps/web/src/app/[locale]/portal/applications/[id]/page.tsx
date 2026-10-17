// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ApplicationDetail } from "@/components/portal/application-detail";
import { PortalSignIn } from "@/components/portal/portal-sign-in";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { portalCall, readPortalToken } from "@/lib/portal/session";
import type { PortalApplicationDetail } from "@/lib/portal/steps";

interface Props {
  params: Promise<{ locale: string; id: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Portal" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/portal/applications/<id>`: one of the candidate's own applications. */
export default async function PortalApplicationPage({ params }: Props) {
  const { locale: raw, id } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(id)) notFound();
  const token = await readPortalToken();
  if (token === null) return <PortalSignIn org={null} />;
  const result = await portalCall<PortalApplicationDetail>(`/v1/hire/portal/applications/${id}`);
  if (!result.ok) {
    if (result.code === "not_found") notFound();
    if (result.code === "not_signed_in") return <PortalSignIn org={token.split(".")[1] ?? null} expired />;
    return <Unavailable retryHref={`/${locale}/portal/applications/${id}`} />;
  }
  return <ApplicationDetail application={result.data} />;
}

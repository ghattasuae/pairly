// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PortalSignIn } from "@/components/portal/portal-sign-in";
import { PrivacyCentre } from "@/components/portal/privacy-centre";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { portalCall, readPortalToken } from "@/lib/portal/session";
import type { PortalPrivacy } from "@/lib/portal/steps";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Portal" });
  return { title: t("privacy.title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/portal/privacy`: the candidate's privacy centre. */
export default async function PortalPrivacyPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const token = await readPortalToken();
  if (token === null) return <PortalSignIn org={null} />;
  const result = await portalCall<PortalPrivacy>("/v1/hire/portal/privacy");
  if (!result.ok) {
    if (result.code === "not_signed_in") return <PortalSignIn org={token.split(".")[1] ?? null} expired />;
    return <Unavailable retryHref={`/${locale}/portal/privacy`} />;
  }
  return <PrivacyCentre privacy={result.data} />;
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PortalLogin } from "@/components/portal/portal-login";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Portal" });
  return { title: t("signIn.title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/portal/login#<magic link>`: the link from the candidate's email. */
export default async function PortalLoginPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <PortalLogin />;
}

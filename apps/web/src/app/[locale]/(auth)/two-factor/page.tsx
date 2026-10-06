// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { TwoFactorVerify } from "@/components/auth/two-factor-verify";
import { redirect } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "TwoFactor" });
  return { title: t("verifyTitle") };
}

export default async function TwoFactorPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  if (await getSession()) redirect({ href: "/", locale });
  return <TwoFactorVerify />;
}

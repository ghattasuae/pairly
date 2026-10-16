// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { TwoFactorVerify } from "@/components/auth/two-factor-verify";
import { redirect } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";
import { safeNextPath } from "@/lib/safe-path";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ next?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "TwoFactor" });
  // `next` can hold an invitation path: never send it on as a referrer.
  return { title: t("verifyTitle"), referrer: "no-referrer" };
}

export default async function TwoFactorPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  if (await getSession()) redirect({ href: "/", locale });
  const next = safeNextPath((await searchParams).next);
  return <TwoFactorVerify next={next} />;
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { AuthCard } from "@/components/auth/auth-card";
import { DEMO_ERRORS, type DemoError, DemoSigninList } from "@/components/auth/demo-signin-list";
import { toLocale } from "@/i18n/routing";
import { demoSigninOn, fetchDemoAccounts } from "@/lib/demo/server";

// The hosted demo's sign-in page (lib/demo/policy.ts). It does not exist unless the
// demo sign-in is on here and in the API.
export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ error?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Demo" });
  return { title: t("title"), robots: { index: false, follow: false } };
}

export default async function DemoSigninPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  if (!demoSigninOn()) notFound();
  const accounts = await fetchDemoAccounts();
  if (accounts.length === 0) notFound();
  const raw = (await searchParams).error;
  const error = DEMO_ERRORS.find((e) => e === raw) ?? null;
  const t = await getTranslations("Demo");
  return (
    <AuthCard title={t("title")} subtitle={t("subtitle")}>
      <DemoSigninList accounts={accounts} locale={locale} error={error as DemoError | null} />
    </AuthCard>
  );
}

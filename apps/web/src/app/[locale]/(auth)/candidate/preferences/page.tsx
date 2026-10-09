// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CandidatePreferences } from "@/components/hire/candidate-preferences";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "CandidatePrefs" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/[locale]/candidate/preferences#<token>`: a candidate's email preferences (Pairly Hire). */
export default async function CandidatePreferencesPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <CandidatePreferences />;
}

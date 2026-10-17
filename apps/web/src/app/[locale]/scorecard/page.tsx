// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ScorecardApp } from "@/components/hire/scorecard";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Scorecard" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/scorecard#<link>`: an interviewer's scorecard from its one-click link. */
export default async function ScorecardPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <ScorecardApp />;
}

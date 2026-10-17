// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { SurveyApp } from "@/components/hire/survey-form";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Survey" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/survey#<link>`: the candidate experience survey from its signed link. */
export default async function SurveyPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <SurveyApp />;
}

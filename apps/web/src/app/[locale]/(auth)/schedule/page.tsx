// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CandidateSchedule } from "@/components/hire/scheduling/candidate-schedule";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "SchedulePage" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/[locale]/schedule#<token>`: a candidate chooses, changes or cancels their interview time. */
export default async function SchedulePage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <CandidateSchedule />;
}

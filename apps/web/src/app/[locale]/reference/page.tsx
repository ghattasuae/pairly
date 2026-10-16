// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { RefereeApp } from "@/components/hire/referee-form";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Referee" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/reference#<link>`: a referee's form from its signed single use link. */
export default async function ReferencePage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <RefereeApp />;
}

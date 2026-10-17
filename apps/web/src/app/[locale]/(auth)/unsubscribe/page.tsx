// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { UnsubscribeLanding } from "@/components/unsubscribe/unsubscribe-landing";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Unsubscribe" });
  return { title: t("titlePlain"), referrer: "no-referrer", robots: { index: false } };
}

/** `/[locale]/unsubscribe#token=...`: an email unsubscribe link opened in a browser. */
export default async function UnsubscribePage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <UnsubscribeLanding />;
}

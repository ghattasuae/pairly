// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { MagicLinkConfirm } from "@/components/auth/magic-link-confirm";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Confirm" });
  return { title: t("title"), referrer: "no-referrer" };
}

export default async function ConfirmPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <MagicLinkConfirm />;
}

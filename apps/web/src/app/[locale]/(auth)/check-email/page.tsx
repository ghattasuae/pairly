// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CheckEmail } from "@/components/auth/check-email";
import { toLocale } from "@/i18n/routing";
import { serverEnv } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "CheckEmail" });
  return { title: t("title") };
}

export default async function CheckEmailPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  const env = serverEnv();
  // The Mailpit hint only makes sense on a local stack.
  const local = ["localhost", "127.0.0.1"].includes(new URL(env.baseUrl).hostname);
  return <CheckEmail showDevHint={local} />;
}

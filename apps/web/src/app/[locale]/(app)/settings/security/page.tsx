// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { TwoFactorSetup } from "@/components/auth/two-factor-setup";
import { PageHeader } from "@/components/states/page-header";
import { toLocale } from "@/i18n/routing";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Nav" });
  return { title: t("security") };
}

/** Personal security settings. Every signed-in user has these, provisioned or not. */
export default async function SecurityPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  const session = await getSession();
  const nav = await getTranslations("Nav");
  const enabled = twoFactorEnabled(session);
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={nav("security")} />
      <TwoFactorSetup enabled={enabled} />
    </div>
  );
}

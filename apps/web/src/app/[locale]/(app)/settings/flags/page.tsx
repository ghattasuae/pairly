// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { FlagsPanel } from "@/components/settings/admin-panels";
import { SettingsFrame } from "@/components/settings/settings-frame";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";
import { canSee } from "@/lib/settings-pages";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Settings" });
  return { title: t("pages.flags.title") };
}

/** Feature flags (`core.flags.read`; switching needs `core.flags.manage`). */
export default async function FlagsPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !canSee("flags", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const flags = await callApi((api) => api.GET("/v1/flags"));
  return (
    <SettingsFrame title={t("pages.flags.title")} subtitle={t("pages.flags.body")}>
      {flags.ok ? (
        <FlagsPanel
          initial={flags.data}
          canManage={viewer.me.permissions.includes("core.flags.manage")}
          totpEnabled={twoFactorEnabled(session)}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/flags`} />
      )}
    </SettingsFrame>
  );
}

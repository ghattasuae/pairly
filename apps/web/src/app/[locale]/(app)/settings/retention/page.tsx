// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { RetentionPanel } from "@/components/settings/retention-panel";
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
  return { title: t("pages.retention.title") };
}

/**
 * Retention (`core.retention.read`; changing it and legal holds need `core.retention.manage`):
 * each rule on or off with its period, the daily cap, and the preview list of what is
 * scheduled for deletion and when.
 */
export default async function RetentionPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !canSee("retention", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const [settings, schedule, holds] = await Promise.all([
    callApi((api) => api.GET("/v1/retention/settings")),
    callApi((api) => api.GET("/v1/retention/schedule")),
    callApi((api) => api.GET("/v1/retention/holds")),
  ]);
  return (
    <SettingsFrame title={t("pages.retention.title")} subtitle={t("pages.retention.body")}>
      {settings.ok && schedule.ok && holds.ok ? (
        <RetentionPanel
          initial={settings.data}
          schedule={schedule.data}
          holds={holds.data}
          canManage={viewer.me.permissions.includes("core.retention.manage")}
          totpEnabled={twoFactorEnabled(session)}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/retention`} />
      )}
    </SettingsFrame>
  );
}

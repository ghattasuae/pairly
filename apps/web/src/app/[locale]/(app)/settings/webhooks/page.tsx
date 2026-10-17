// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { SettingsFrame } from "@/components/settings/settings-frame";
import { WebhooksPanel } from "@/components/settings/webhooks-panel";
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
  return { title: t("pages.webhooks.title") };
}

/** Webhook endpoints (`core.webhooks.read`; changes need `core.webhooks.manage`). */
export default async function WebhooksPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !canSee("webhooks", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const [endpoints, types] = await Promise.all([
    callApi((api) => api.GET("/v1/webhooks/endpoints")),
    callApi((api) => api.GET("/v1/webhooks/event-types")),
  ]);
  return (
    <SettingsFrame title={t("pages.webhooks.title")} subtitle={t("pages.webhooks.body")}>
      {endpoints.ok ? (
        <WebhooksPanel
          initial={endpoints.data}
          eventTypes={types.ok ? types.data : []}
          canManage={viewer.me.permissions.includes("core.webhooks.manage")}
          totpEnabled={twoFactorEnabled(session)}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/webhooks`} />
      )}
    </SettingsFrame>
  );
}

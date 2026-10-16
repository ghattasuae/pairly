// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ApiKeysPanel } from "@/components/settings/admin-panels";
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
  return { title: t("pages.apiKeys.title") };
}

const KEY_ACTIONS = new Set(["read", "propose", "upload"]);

/** API keys (`core.api_keys.manage`). A key can only hold read, propose and upload scopes. */
export default async function ApiKeysPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !canSee("apiKeys", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const keys = await callApi((api) => api.GET("/v1/api-keys"));
  // Offer the scopes a key may hold that the person holds too; the API checks again.
  const scopes = viewer.me.permissions.filter((p) => KEY_ACTIONS.has(p.split(".").at(-1) ?? "")).sort();
  return (
    <SettingsFrame title={t("pages.apiKeys.title")} subtitle={t("pages.apiKeys.body")}>
      {keys.ok ? (
        <ApiKeysPanel
          initial={keys.data}
          scopes={scopes}
          totpEnabled={twoFactorEnabled(session)}
          sandbox={viewer.me.org.sandbox}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/api-keys`} />
      )}
    </SettingsFrame>
  );
}

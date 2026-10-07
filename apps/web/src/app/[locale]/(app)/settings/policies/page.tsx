// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PoliciesPanel } from "@/components/settings/policies-panel";
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
  return { title: t("pages.policies.title") };
}

/** Policy documents and rule sets (`core.policy.read`; changes need `core.policy.manage`). */
export default async function PoliciesPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !canSee("policies", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const [documents, ruleSets] = await Promise.all([
    callApi((api) => api.GET("/v1/policies/documents")),
    callApi((api) => api.GET("/v1/policies/rule-sets")),
  ]);
  return (
    <SettingsFrame title={t("pages.policies.title")} subtitle={t("pages.policies.body")}>
      {documents.ok ? (
        <PoliciesPanel
          documents={documents.data}
          ruleSets={ruleSets.ok ? ruleSets.data : []}
          meId={viewer.me.id}
          canManage={viewer.me.permissions.includes("core.policy.manage")}
          totpEnabled={twoFactorEnabled(session)}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/policies`} />
      )}
    </SettingsFrame>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { NotificationPreferences } from "@/components/settings/notification-preferences";
import { SettingsFrame } from "@/components/settings/settings-frame";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Settings" });
  return { title: t("pages.notifications.title") };
}

/** The signed-in person's own notification preferences (the unsubscribe page links here). */
export default async function NotificationSettingsPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const t = await getTranslations("Settings");
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const prefs = viewer.status === "ready" ? await callApi((api) => api.GET("/v1/notifications/preferences")) : null;
  return (
    <SettingsFrame title={t("pages.notifications.title")} subtitle={t("pages.notifications.body")}>
      {prefs?.ok ? (
        <NotificationPreferences initial={prefs.data} />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/notifications`} />
      )}
    </SettingsFrame>
  );
}

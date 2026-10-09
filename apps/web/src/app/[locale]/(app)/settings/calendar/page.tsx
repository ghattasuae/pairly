// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CalendarSettings } from "@/components/settings/calendar-settings";
import { SettingsFrame } from "@/components/settings/settings-frame";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { resolveViewer } from "@/lib/provisioning";
import { canSee } from "@/lib/settings-pages";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Settings" });
  return { title: t("pages.calendar.title") };
}

const one = (value: string | string[] | undefined): string | null =>
  typeof value === "string" && value.length > 0 && value.length <= 512 ? value : null;

/** Calendar connections and interview availability (`hire.interview.read`). */
export default async function CalendarSettingsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, query] = await Promise.all([resolveViewer(), searchParams]);
  if (viewer.status !== "ready" || !canSee("calendar", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const [integrations, availability] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/scheduling/integrations")),
    callApi((api) => api.GET("/v1/hire/scheduling/availability/me")),
  ]);
  const code = one(query.code);
  const state = one(query.state);
  return (
    <SettingsFrame title={t("pages.calendar.title")} subtitle={t("pages.calendar.body")}>
      {integrations.ok ? (
        <CalendarSettings
          initialIntegrations={integrations.data}
          initialAvailability={availability.ok ? availability.data : null}
          callback={code && state ? { code, state } : null}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/calendar`} />
      )}
    </SettingsFrame>
  );
}

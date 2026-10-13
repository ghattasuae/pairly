// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CalendarOrgSettings } from "@/components/settings/calendar-org-settings";
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

/** Calendar connections and interview availability (`hire.interview.read`); for
 * administrators (`hire.settings.manage`) also the organisation's Google, Microsoft and
 * Zoom set-up and the external calendar writes waiting for review. */
export default async function CalendarSettingsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, query] = await Promise.all([resolveViewer(), searchParams]);
  if (viewer.status !== "ready" || !canSee("calendar", viewer.me.permissions)) notFound();
  const t = await getTranslations("Settings");
  const admin = viewer.me.permissions.includes("hire.settings.manage");
  const [integrations, availability, org, operations] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/scheduling/integrations")),
    callApi((api) => api.GET("/v1/hire/scheduling/availability/me")),
    admin ? callApi((api) => api.GET("/v1/hire/settings/scheduling/integrations")) : null,
    admin ? callApi((api) => api.GET("/v1/hire/settings/scheduling/operations")) : null,
  ]);
  const code = one(query.code);
  const state = one(query.state);
  // Microsoft's redirect carries `session_state`, Google's `scope` (a hint only: the page
  // remembers which button was pressed).
  const hint = one(query.session_state) ? "microsoft" : "google";
  return (
    <SettingsFrame title={t("pages.calendar.title")} subtitle={t("pages.calendar.body")}>
      <div className="space-y-10">
      {integrations.ok ? (
        <CalendarSettings
          initialIntegrations={integrations.data}
          initialAvailability={availability.ok ? availability.data : null}
          callback={code && state ? { code, state, hint } : null}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/calendar`} />
      )}
      {org?.ok && <CalendarOrgSettings initial={org.data} operations={operations?.ok ? operations.data : null} />}
      </div>
    </SettingsFrame>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PaySettingsView } from "@/components/pay/pay-settings";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.settings" });
  return { title: t("title") };
}

/**
 * Pay settings for admins and finance controllers (`pay.settings.manage`): cooling-off,
 * the invoice checks and rule approval, through `/v1/pay/settings`.
 */
export default async function PaySettingsPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.settings.manage", "/pay/settings");
  if (!gate.ok) return gate.page;
  const settings = await callApi((api) => api.GET("/v1/pay/settings"));
  if (!settings.ok) return <PayUnavailable retry={`/${locale}/pay/settings`} />;
  const t = await getTranslations("Pay.settings");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="settings" permissions={gate.permissions} />
      <PaySettingsView initial={settings.data} totpEnabled={gate.totpEnabled} />
    </PayFrame>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PayForecastView } from "@/components/pay/pay-forecast";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { forecastWeeks } from "@/lib/pay/forecast";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.forecast" });
  return { title: t("title") };
}

/**
 * The cash forecast for finance controllers and admins (`pay.analytics.read`): the next
 * weeks of payables per currency. The number of weeks lives in the URL.
 */
export default async function PayForecastPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.analytics.read", "/pay/forecast");
  if (!gate.ok) return gate.page;
  const weeks = forecastWeeks(await searchParams);
  const result = await callApi((api) => api.GET("/v1/pay/forecast", { params: { query: { weeks } } }));
  if (!result.ok) return <PayUnavailable retry={`/${locale}/pay/forecast`} />;
  const t = await getTranslations("Pay.forecast");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="forecast" permissions={gate.permissions} />
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm">
        <label className="flex flex-col gap-1">
          {t("weeks")}
          <input
            type="number"
            name="weeks"
            min={1}
            max={26}
            defaultValue={weeks}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </label>
        <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground">
          {t("apply")}
        </button>
      </form>
      <PayForecastView data={result.data} />
    </PayFrame>
  );
}

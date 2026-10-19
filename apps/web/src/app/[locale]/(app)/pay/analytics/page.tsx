// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PayAnalyticsView } from "@/components/pay/pay-analytics";
import { buttonVariants } from "@/components/ui/button";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { payAnalyticsQuery } from "@/lib/pay/analytics";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.analytics" });
  return { title: t("title") };
}

/**
 * Pay analytics for finance controllers and admins (`pay.analytics.read`, brief 4.9).
 * The period lives in the URL (a plain GET form), so a view can be shared and reloaded.
 */
export default async function PayAnalyticsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.analytics.read", "/pay/analytics");
  if (!gate.ok) return gate.page;
  const query = payAnalyticsQuery(await searchParams);
  const result = await callApi((api) => api.GET("/v1/pay/analytics", { params: { query } }));
  if (!result.ok) return <PayUnavailable retry={`/${locale}/pay/analytics`} />;
  const t = await getTranslations("Pay.analytics");
  const field = "rounded-md border bg-background px-2 py-1.5 text-sm";
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="analytics" permissions={gate.permissions} />
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm" aria-label={t("filters")}>
        <label className="flex flex-col gap-1">
          {t("since")}
          <input type="date" name="since" defaultValue={query.since ?? ""} className={field} />
        </label>
        <label className="flex flex-col gap-1">
          {t("until")}
          <input type="date" name="until" defaultValue={query.until ?? ""} className={field} />
        </label>
        <button type="submit" className={buttonVariants({ variant: "outline", size: "sm" })}>
          {t("apply")}
        </button>
      </form>
      <PayAnalyticsView data={result.data} />
    </PayFrame>
  );
}

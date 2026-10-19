// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PayMonthEndView } from "@/components/pay/pay-month-end";
import { buttonVariants } from "@/components/ui/button";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { monthEndPeriod } from "@/lib/pay/month-end";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.monthEnd" });
  return { title: t("title") };
}

/**
 * The month-end report (`pay.month_end.read`): open payables and aging at the period
 * end, accrual suggestions, open exceptions, the CSV and the journal export. The
 * period (YYYY-MM, default the previous month) lives in the URL.
 */
export default async function PayMonthEndPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.month_end.read", "/pay/month-end");
  if (!gate.ok) return gate.page;
  const period = monthEndPeriod(await searchParams);
  const result = await callApi((api) => api.GET("/v1/pay/month-end", { params: { query: { period } } }));
  if (!result.ok) return <PayUnavailable retry={`/${locale}/pay/month-end`} />;
  const exports = await callApi((api) => api.GET("/v1/pay/journal/exports", { params: { query: { period } } }));
  const t = await getTranslations("Pay.monthEnd");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="monthEnd" permissions={gate.permissions} />
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm">
        <label className="flex flex-col gap-1">
          {t("period")}
          <input
            type="month"
            name="period"
            min="2000-01"
            max="2100-12"
            defaultValue={period}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </label>
        <button type="submit" className={buttonVariants({ variant: "outline", size: "sm" })}>
          {t("apply")}
        </button>
      </form>
      <PayMonthEndView data={result.data} exports={exports.ok ? exports.data : []} />
    </PayFrame>
  );
}

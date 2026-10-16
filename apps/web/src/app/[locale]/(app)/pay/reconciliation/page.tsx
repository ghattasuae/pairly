// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PayReconciliationView } from "@/components/pay/pay-reconciliation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { reconciliationDays } from "@/lib/pay/reconciliation";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.reconciliation" });
  return { title: t("title") };
}

/**
 * Bank reconciliation (`pay.reconciliation.read`): imported statements matched with
 * paid invoices and payment runs. The number of days after which a paid invoice with
 * no statement line is listed lives in the URL.
 */
export default async function PayReconciliationPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.reconciliation.read", "/pay/reconciliation");
  if (!gate.ok) return gate.page;
  const days = reconciliationDays(await searchParams);
  const result = await callApi((api) => api.GET("/v1/pay/reconciliation", { params: { query: { days } } }));
  if (!result.ok) return <PayUnavailable retry={`/${locale}/pay/reconciliation`} />;
  const t = await getTranslations("Pay.reconciliation");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="reconciliation" permissions={gate.permissions} />
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm">
        <label className="flex flex-col gap-1">
          {t("days")}
          <input
            type="number"
            name="days"
            min={1}
            max={90}
            defaultValue={days}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </label>
        <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground">
          {t("apply")}
        </button>
      </form>
      <PayReconciliationView data={result.data} />
    </PayFrame>
  );
}

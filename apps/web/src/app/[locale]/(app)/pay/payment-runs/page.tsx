// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PaymentRunList } from "@/components/pay/payment-runs";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { runStatusFilter } from "@/lib/pay/payments";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ status?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.runs" });
  return { title: t("title") };
}

/** Payment runs (brief 4.6.3): propose one from approved invoices, then follow it. */
export default async function PaymentRunsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.payment_run.read", "/pay/payment-runs");
  if (!gate.ok) return gate.page;
  const status = runStatusFilter((await searchParams).status);
  const canCreate = gate.permissions.includes("pay.payment_run.create");
  const [list, settings] = await Promise.all([
    callApi((api) => api.GET("/v1/pay/payment-runs", { params: { query: { limit: 200, ...(status ? { status } : {}) } } })),
    canCreate ? callApi((api) => api.GET("/v1/pay/settings")) : Promise.resolve(null),
  ]);
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay/payment-runs`} />;
  const limit = settings?.ok ? settings.data.payments?.default_cash_limit : null;
  const t = await getTranslations("Pay.runs");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="runs" permissions={gate.permissions} />
      <PaymentRunList
        items={list.data.items}
        counts={list.data.counts}
        status={status}
        canCreate={canCreate}
        defaultCashLimit={typeof limit === "string" ? limit : null}
      />
    </PayFrame>
  );
}

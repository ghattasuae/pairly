// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { BankChangeList } from "@/components/pay/bank-change";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.changes" });
  return { title: t("title") };
}

/** Bank-detail change tickets (brief 4.1.4): open ones first, each with its checklist. */
export default async function BankChangesPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.bank_change.read", "/pay/bank-changes");
  if (!gate.ok) return gate.page;
  const list = await callApi((api) => api.GET("/v1/pay/bank-changes", { params: { query: { limit: 200 } } }));
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay/bank-changes`} />;
  const t = await getTranslations("Pay.changes");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="changes" permissions={gate.permissions} />
      <BankChangeList items={list.data.items} />
    </PayFrame>
  );
}

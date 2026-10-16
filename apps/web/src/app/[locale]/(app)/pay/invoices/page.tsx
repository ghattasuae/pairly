// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InvoiceList } from "@/components/pay/invoices";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { statusFilter } from "@/lib/pay/invoices";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ status?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.invoices" });
  return { title: t("title") };
}

/** Invoices received by email, upload and API, filtered by status (brief 4.2). */
export default async function InvoicesPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.invoice.read", "/pay/invoices");
  if (!gate.ok) return gate.page;
  const status = statusFilter((await searchParams).status);
  const list = await callApi((api) =>
    api.GET("/v1/pay/invoices", { params: { query: { limit: 200, ...(status ? { status } : {}) } } }),
  );
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay/invoices`} />;
  const t = await getTranslations("Pay.invoices");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="invoices" permissions={gate.permissions} />
      <InvoiceList items={list.data.items} counts={list.data.counts} status={status} />
    </PayFrame>
  );
}

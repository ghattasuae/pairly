// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ExceptionsQueue } from "@/components/pay/exceptions";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { isCategory } from "@/lib/pay/exceptions";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ category?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.exceptions" });
  return { title: t("title") };
}

/** The exceptions queue (brief 4.5.4): invoices waiting for a person, by category. */
export default async function ExceptionsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.invoice.read", "/pay/exceptions");
  if (!gate.ok) return gate.page;
  const raw = (await searchParams).category;
  const category = typeof raw === "string" && isCategory(raw) ? raw : null;
  const data = await callApi((api) => api.GET("/v1/pay/exceptions", { params: { query: { limit: 200 } } }));
  if (!data.ok) return <PayUnavailable retry={`/${locale}/pay/exceptions`} />;
  const t = await getTranslations("Pay.exceptions");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="exceptions" permissions={gate.permissions} />
      <ExceptionsQueue
        data={data.data}
        locale={locale}
        initialCategory={category}
      />
    </PayFrame>
  );
}

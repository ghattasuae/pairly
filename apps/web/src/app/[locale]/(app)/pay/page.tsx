// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { VendorsView } from "@/components/pay/vendors";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { sectionMetadata } from "../section-placeholder";
import { PayFrame, PayHeader, PayUnavailable, payGate } from "./gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "pay");
}

/** The vendor master (brief 4.1): search, pending onboardings, payment holds, add. */
export default async function PayPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.vendor.read", "/pay");
  if (!gate.ok) return gate.page;
  const list = await callApi((api) => api.GET("/v1/pay/vendors", { params: { query: { limit: 200 } } }));
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay`} />;
  const t = await getTranslations("Pay");
  return (
    <PayFrame>
      <PayHeader title={t("vendors.title")} intro={t("vendors.intro")} current="vendors" permissions={gate.permissions} />
      <VendorsView initial={list.data.items} canWrite={gate.permissions.includes("pay.vendor.write")} />
    </PayFrame>
  );
}

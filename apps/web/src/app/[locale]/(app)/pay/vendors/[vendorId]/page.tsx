// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { VendorDetailView } from "@/components/pay/vendor-detail";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, UUID_RE, payGate } from "../../gate";

interface Props {
  params: Promise<{ locale: string; vendorId: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.vendor" });
  return { title: t("title") };
}

/** One vendor: record, masked bank details, risk profile, hold and change tickets. */
export default async function VendorPage({ params }: Props) {
  const { locale: raw, vendorId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(vendorId)) notFound();
  const path = `/pay/vendors/${vendorId}`;
  const gate = await payGate(locale, "pay.vendor.read", path);
  if (!gate.ok) return gate.page;
  const vendor = await callApi((api) =>
    api.GET("/v1/pay/vendors/{vendor_id}", { params: { path: { vendor_id: vendorId } } }),
  );
  if (!vendor.ok) {
    if (vendor.status === 404) notFound();
    return <PayUnavailable retry={`/${locale}${path}`} />;
  }
  return (
    <PayFrame>
      <PayHeader title={vendor.data.legal_name} current={null} permissions={gate.permissions} />
      <VendorDetailView initial={vendor.data} totpEnabled={gate.totpEnabled} returnPath={`/${locale}${path}`} />
    </PayFrame>
  );
}

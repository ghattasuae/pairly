// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PurchaseOrderList } from "@/components/pay/purchase-orders";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.purchaseOrders" });
  return { title: t("title") };
}

/**
 * Purchase orders (brief 4.5.1): the list, a CSV import of POs and goods receipts, and
 * closing or cancelling an open PO (cancel needs a second factor).
 */
export default async function PurchaseOrdersPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.purchase_order.read", "/pay/purchase-orders");
  if (!gate.ok) return gate.page;
  const list = await callApi((api) => api.GET("/v1/pay/purchase-orders", { params: { query: { limit: 200 } } }));
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay/purchase-orders`} />;
  const t = await getTranslations("Pay.purchaseOrders");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="orders" permissions={gate.permissions} />
      <PurchaseOrderList
        items={list.data.items}
        canImport={gate.permissions.includes("pay.purchase_order.write")}
        canClose={gate.permissions.includes("pay.purchase_order.write")}
        canCancel={gate.permissions.includes("pay.purchase_order.manage")}
        totpEnabled={gate.totpEnabled}
      />
    </PayFrame>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { Unavailable } from "@/components/states/unavailable";
import { VendorInvoiceDetail } from "@/components/vendor-portal/vendor-invoice-detail";
import { toLocale } from "@/i18n/routing";
import { vendorPortalCall } from "@/lib/vendor-portal/session";
import type { VendorPortalInvoiceDetail } from "@/lib/vendor-portal/types";

interface Props {
  params: Promise<{ locale: string; id: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "VendorPortal" });
  return { title: t("detail.title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/vendor-portal/invoices/<id>`: one of the vendor's own invoices. */
export default async function VendorInvoicePage({ params }: Props) {
  const { locale: raw, id } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(id)) notFound();
  const result = await vendorPortalCall<VendorPortalInvoiceDetail>(`/v1/pay/vendor-portal/invoices/${id}`);
  if (!result.ok) {
    if (result.code === "not_signed_in") redirect(`/${locale}/vendor-portal`);
    if (result.code === "not_found") notFound();
    return <Unavailable retryHref={`/${locale}/vendor-portal/invoices/${id}`} />;
  }
  return <VendorInvoiceDetail invoice={result.data} />;
}

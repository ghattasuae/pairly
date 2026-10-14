// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InvoiceCoding } from "@/components/pay/invoice-coding";
import { InvoiceReview } from "@/components/pay/invoice-review";
import { PageHeader } from "@/components/states/page-header";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayUnavailable, UUID_RE, payGate } from "../../gate";

interface Props {
  params: Promise<{ locale: string; invoiceId: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.invoice" });
  return { title: t("title") };
}

/**
 * One invoice side by side: the document and where each value was read (brief 4.3.3),
 * then the GL coding of its lines with suggestions (brief 4.5.3).
 */
export default async function InvoicePage({ params }: Props) {
  const { locale: raw, invoiceId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(invoiceId)) notFound();
  const path = `/pay/invoices/${invoiceId}`;
  const gate = await payGate(locale, "pay.invoice.read", path);
  if (!gate.ok) return gate.page;
  const invoice = await callApi((api) =>
    api.GET("/v1/pay/invoices/{invoice_id}", { params: { path: { invoice_id: invoiceId } } }),
  );
  if (!invoice.ok) {
    if (invoice.status === 404) notFound();
    return <PayUnavailable retry={`/${locale}${path}`} />;
  }
  const canLink = invoice.data.can_process && gate.permissions.includes("pay.vendor.read");
  const vendors = canLink
    ? await callApi((api) => api.GET("/v1/pay/vendors", { params: { query: { limit: 200, status: "active" } } }))
    : null;
  const coding = await callApi((api) =>
    api.GET("/v1/pay/invoices/{invoice_id}/coding", { params: { path: { invoice_id: invoiceId } } }),
  );
  const t = await getTranslations("Pay.invoice");
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 px-5 py-8 sm:px-8">
      <PageHeader
        eyebrow={t("eyebrow")}
        title={<bdi>{invoice.data.invoice_number ?? invoice.data.reference}</bdi>}
      >
        <p className="text-sm text-muted-foreground">{invoice.data.reference}</p>
      </PageHeader>
      <InvoiceReview
        initial={invoice.data}
        vendors={
          vendors?.ok
            ? vendors.data.items.map((v) => ({ id: v.id, legal_name: v.legal_name, reference: v.reference }))
            : []
        }
      />
      {coding.ok && <InvoiceCoding initial={coding.data} locale={locale} />}
    </div>
  );
}

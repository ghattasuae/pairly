// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { BankChangeView } from "@/components/pay/bank-change";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, UUID_RE, payGate } from "../../gate";

interface Props {
  params: Promise<{ locale: string; ticketId: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.change" });
  return { title: t("title") };
}

/**
 * One bank-detail change: old and new details side by side, the call back checklist
 * (the number on file only), the dual approval and the cooling-off period.
 */
export default async function BankChangePage({ params }: Props) {
  const { locale: raw, ticketId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(ticketId)) notFound();
  const path = `/pay/bank-changes/${ticketId}`;
  const gate = await payGate(locale, "pay.bank_change.read", path);
  if (!gate.ok) return gate.page;
  const ticket = await callApi((api) =>
    api.GET("/v1/pay/bank-changes/{ticket_id}", { params: { path: { ticket_id: ticketId } } }),
  );
  if (!ticket.ok) {
    if (ticket.status === 404) notFound();
    return <PayUnavailable retry={`/${locale}${path}`} />;
  }
  const t = await getTranslations("Pay.change");
  return (
    <PayFrame>
      <PayHeader
        title={t("heading", { reference: ticket.data.reference })}
        current="changes"
        permissions={gate.permissions}
      />
      <BankChangeView initial={ticket.data} totpEnabled={gate.totpEnabled} returnPath={`/${locale}${path}`} />
    </PayFrame>
  );
}

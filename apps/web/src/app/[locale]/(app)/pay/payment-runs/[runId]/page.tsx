// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PaymentRunView } from "@/components/pay/payment-runs";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, UUID_RE, payGate } from "../../gate";

interface Props {
  params: Promise<{ locale: string; runId: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.runs" });
  return { title: t("detailTitle") };
}

/** One payment run: its invoices, its approval, the bank file and marking paid. */
export default async function PaymentRunPage({ params }: Props) {
  const { locale: raw, runId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(runId)) notFound();
  const path = `/pay/payment-runs/${runId}`;
  const gate = await payGate(locale, "pay.payment_run.read", path);
  if (!gate.ok) return gate.page;
  const run = await callApi((api) => api.GET("/v1/pay/payment-runs/{run_id}", { params: { path: { run_id: runId } } }));
  if (!run.ok) {
    if (run.status === 404) notFound();
    return <PayUnavailable retry={`/${locale}${path}`} />;
  }
  const t = await getTranslations("Pay.runs");
  return (
    <PayFrame>
      <PayHeader title={t("detailHeading", { reference: run.data.reference })} current="runs" permissions={gate.permissions} />
      <PaymentRunView initial={run.data} totpEnabled={gate.totpEnabled} />
    </PayFrame>
  );
}

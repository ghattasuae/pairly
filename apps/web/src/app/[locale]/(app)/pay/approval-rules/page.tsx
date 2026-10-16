// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ApprovalRulesView } from "@/components/pay/approval-rules";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.rules" });
  return { title: t("title") };
}

/** The approval rules in force, read only, with their versions (policy as code). */
export default async function ApprovalRulesPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.invoice.read", "/pay/approval-rules");
  if (!gate.ok) return gate.page;
  const rules = await callApi((api) => api.GET("/v1/pay/approval-rules"));
  if (!rules.ok) return <PayUnavailable retry={`/${locale}/pay/approval-rules`} />;
  const t = await getTranslations("Pay.rules");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="rules" permissions={gate.permissions} />
      <ApprovalRulesView items={rules.data.items} />
    </PayFrame>
  );
}

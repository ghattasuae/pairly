// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InvitesView } from "@/components/pay/invites";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";

import { PayFrame, PayHeader, PayUnavailable, payGate } from "../gate";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Pay.invites" });
  return { title: t("title") };
}

/** Invite vendors to fill in their own details through a signed single use link. */
export default async function InvitesPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const gate = await payGate(locale, "pay.vendor.write", "/pay/invites");
  if (!gate.ok) return gate.page;
  const list = await callApi((api) => api.GET("/v1/pay/vendor-invites"));
  if (!list.ok) return <PayUnavailable retry={`/${locale}/pay/invites`} />;
  const t = await getTranslations("Pay.invites");
  return (
    <PayFrame>
      <PayHeader title={t("title")} intro={t("intro")} current="invites" permissions={gate.permissions} />
      <InvitesView initial={list.data.items} />
    </PayFrame>
  );
}

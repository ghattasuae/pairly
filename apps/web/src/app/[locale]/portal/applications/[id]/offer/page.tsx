// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { OfferView } from "@/components/portal/offer-view";
import { PortalSignIn } from "@/components/portal/portal-sign-in";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import type { HirePortalOffer } from "@/lib/hire/types";
import { portalCall, readPortalToken } from "@/lib/portal/session";

interface Props {
  params: Promise<{ locale: string; id: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "PortalOffer" });
  return { title: t("pageTitle"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/portal/applications/<id>/offer`: the candidate's offer, once it was sent. */
export default async function PortalOfferPage({ params }: Props) {
  const { locale: raw, id } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(id)) notFound();
  const token = await readPortalToken();
  if (token === null) return <PortalSignIn org={null} />;
  const result = await portalCall<HirePortalOffer>(`/v1/hire/portal/applications/${id}/offer`);
  if (!result.ok) {
    if (result.code === "not_found") notFound();
    if (result.code === "not_signed_in") return <PortalSignIn org={token.split(".")[1] ?? null} expired />;
    return <Unavailable retryHref={`/${locale}/portal/applications/${id}/offer`} />;
  }
  return <OfferView initial={result.data} />;
}

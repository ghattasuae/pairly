// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { Unavailable } from "@/components/states/unavailable";
import { VendorDetails } from "@/components/vendor-portal/vendor-details";
import { toLocale } from "@/i18n/routing";
import { vendorPortalCall } from "@/lib/vendor-portal/session";
import type { VendorPortalDetails } from "@/lib/vendor-portal/types";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "VendorPortal" });
  return { title: t("details.title"), referrer: "no-referrer", robots: { index: false } };
}

/** `/<locale>/vendor-portal/details`: verified details, contacts and bank change requests. */
export default async function VendorDetailsPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const result = await vendorPortalCall<VendorPortalDetails>("/v1/pay/vendor-portal/details");
  if (!result.ok) {
    if (result.code === "not_signed_in") redirect(`/${locale}/vendor-portal`);
    return <Unavailable retryHref={`/${locale}/vendor-portal/details`} />;
  }
  return <VendorDetails details={result.data} />;
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { Unavailable } from "@/components/states/unavailable";
import { VendorPortalHome } from "@/components/vendor-portal/vendor-home";
import { VendorPortalSignIn } from "@/components/vendor-portal/vendor-sign-in";
import { toLocale } from "@/i18n/routing";
import { readVendorPortalToken, vendorPortalCall } from "@/lib/vendor-portal/session";
import { ORG_RE, type VendorPortalInvoice, type VendorPortalMe } from "@/lib/vendor-portal/types";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "VendorPortal" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/<locale>/vendor-portal`: the vendor's invoices, or the sign-in form (a magic link by
 * email) when there is no portal session. `?org=<organisation id>` names the organisation
 * for the sign-in form (accounts payable shares that link).
 */
export default async function VendorPortalPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const query = await searchParams;
  const rawOrg = Array.isArray(query.org) ? query.org[0] : query.org;
  const org = rawOrg && ORG_RE.test(rawOrg) ? rawOrg : null;
  const token = await readVendorPortalToken();
  if (token === null) return <VendorPortalSignIn org={org} />;
  const [me, invoices] = await Promise.all([
    vendorPortalCall<VendorPortalMe>("/v1/pay/vendor-portal/me"),
    vendorPortalCall<VendorPortalInvoice[]>("/v1/pay/vendor-portal/invoices"),
  ]);
  if (!me.ok || !invoices.ok) {
    if ((!me.ok && me.code === "not_signed_in") || (!invoices.ok && invoices.code === "not_signed_in")) {
      // The ended session still names its organisation: offer a new link there.
      return <VendorPortalSignIn org={org ?? token.split(".")[1] ?? null} expired />;
    }
    return <Unavailable retryHref={`/${locale}/vendor-portal`} />;
  }
  return <VendorPortalHome me={me.data} invoices={invoices.data} />;
}

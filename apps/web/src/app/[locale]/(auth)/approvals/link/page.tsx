// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ApprovalLinkLanding } from "@/components/approvals/approval-link-landing";
import { toLocale } from "@/i18n/routing";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { ssoAvailability } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "ApprovalLink" });
  return { title: t("titlePlain"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/[locale]/approvals/link#<token>`, linked from approval emails and Slack (the API
 * builds `/approvals/link#<token>`; the locale is added by the locale redirect and the
 * browser keeps the fragment). The token never reaches a server log. Opening the page
 * changes nothing; only the explicit confirm button decides.
 */
export default async function ApprovalLinkPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  const session = await getSession();
  return <ApprovalLinkLanding sso={ssoAvailability()} totpEnabled={twoFactorEnabled(session)} />;
}

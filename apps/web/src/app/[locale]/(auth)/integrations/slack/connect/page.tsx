// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { SlackConnectLanding } from "@/components/integrations/slack-connect-landing";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";
import { ssoAvailability } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "SlackConnect" });
  return { title: t("title"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/[locale]/integrations/slack/connect#<token>`, linked from the `/pairly connect` answer
 * in Slack (the API builds `/integrations/slack/connect#<token>`; the locale is added by
 * the locale redirect and the browser keeps the fragment). The token never reaches a
 * server log. Opening the page changes nothing; only the confirm button links the accounts.
 */
export default async function SlackConnectPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  const session = await getSession();
  return <SlackConnectLanding sso={ssoAvailability()} email={session?.user.email ?? null} />;
}

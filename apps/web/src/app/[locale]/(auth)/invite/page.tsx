// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InviteLanding } from "@/components/invite/invite-landing";
import { toLocale } from "@/i18n/routing";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Invite" });
  return { title: t("titlePlain"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/[locale]/invite#token=...`, linked from the invitation email. The token stays in the
 * fragment (never sent to a server). Opening the page changes nothing (mail scanners open
 * links): the person signs in with a magic link to the invited address if needed, then
 * presses "Accept and join", which calls the API.
 */
export default async function InvitePage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <InviteLanding />;
}

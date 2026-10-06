// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InvitePanel, type InviteView } from "@/components/invite/invite-panel";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";
import { previewInvitation } from "@/lib/invitations";

interface Props {
  params: Promise<{ locale: string; token: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Invite" });
  // The token is in this URL: never send it on as a referrer, never index the page.
  return { title: t("titlePlain"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/[locale]/invite/[token]`, linked from the invitation email. Opening it changes nothing
 * (mail scanners open links): the person signs in with a magic link to the invited
 * address if needed, then presses "Accept and join", which calls the API.
 */
export default async function InvitePage({ params }: Props) {
  const { locale: raw, token } = await params;
  setRequestLocale(toLocale(raw));
  const [found, session] = await Promise.all([previewInvitation(token), getSession()]);

  let view: InviteView;
  if (found.status !== "valid") {
    view = { kind: found.status };
  } else {
    const preview = { ...found.preview, expires_at: String(found.preview.expires_at) };
    const current = session?.user.email ?? null;
    if (!current) view = { kind: "signIn", preview };
    else if (current.toLowerCase() === preview.email.toLowerCase()) view = { kind: "accept", preview, currentEmail: current };
    else view = { kind: "wrongUser", preview, currentEmail: current };
  }
  return <InvitePanel token={token} view={view} />;
}

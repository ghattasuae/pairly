// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { MembersPanel } from "@/components/members/members-panel";
import { PageHeader } from "@/components/states/page-header";
import { SecondFactorNeeded } from "@/components/states/second-factor-needed";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { getApiClient } from "@/lib/api/server";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";
import { secondFactorNeed } from "@/lib/viewer-state";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Members" });
  return { title: t("title") };
}

/**
 * Members of the active organisation and its invitations (`core.users.read`); the invite
 * form and withdraw buttons for `core.users.manage`. Hidden pages 404, and the API checks
 * every call on its own.
 */
export default async function MembersPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready" || !viewer.me.permissions.includes("core.users.read")) notFound();
  const { me } = viewer;
  const t = await getTranslations("Members");

  const client = await getApiClient();
  const [members, invitations] = await Promise.all([client.GET("/v1/members"), client.GET("/v1/invitations")]);
  const canManage = me.permissions.includes("core.users.manage");
  const need = canManage ? secondFactorNeed(me, twoFactorEnabled(session)) : null;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader eyebrow={me.org.name} title={t("title")}>
        <p>{t("subtitle", { org: me.org.name })}</p>
      </PageHeader>
      {need && <SecondFactorNeeded need={need} />}
      {members.data && invitations.data ? (
        <MembersPanel
          currentUserId={me.id}
          members={members.data.items}
          invitations={invitations.data.items}
          canManage={canManage}
          blockedBySecondFactor={canManage && !me.second_factor}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/settings/members`} />
      )}
    </div>
  );
}

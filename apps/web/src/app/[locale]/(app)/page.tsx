// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import type * as React from "react";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ActionNeeded } from "@/components/states/action-needed";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Badge } from "@/components/ui/badge";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Nav" });
  return { title: t("home") };
}

export default async function HomePage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const t = await getTranslations("Home");
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);

  return (
    <div className="mx-auto w-full max-w-5xl space-y-10 px-5 py-10 sm:px-8">
      {viewer.status === "ready" && (
        <>
          <PageHeader
            eyebrow={viewer.me.org.name}
            title={viewer.me.display_name ? t("greeting", { name: viewer.me.display_name }) : t("greetingNoName")}
          >
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <span className="text-sm">{t("rolesLabel")}</span>
              {viewer.me.roles.map((role) => (
                <Badge key={`${role.role}:${role.workflow ?? "all"}`} variant="outline">
                  {role.workflow ? `${role.role} · ${role.workflow}` : role.role}
                </Badge>
              ))}
            </div>
          </PageHeader>
          <div className="rise" style={{ "--rise-index": 1 } as React.CSSProperties}>
            <ActionNeeded count={0} />
          </div>
        </>
      )}
      {viewer.status === "not_provisioned" && <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />}
      {(viewer.status === "unavailable" || viewer.status === "unauthenticated") && <Unavailable retryHref={`/${locale}`} />}
    </div>
  );
}

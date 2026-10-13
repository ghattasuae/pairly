// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import type * as React from "react";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { RoleDashboard } from "@/components/home/role-dashboards";
import { ActionNeeded } from "@/components/states/action-needed";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { BackupCodeNotice } from "@/components/states/backup-code-notice";
import { SecondFactorNeeded } from "@/components/states/second-factor-needed";
import { Unavailable } from "@/components/states/unavailable";
import { Badge } from "@/components/ui/badge";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { signedInWithBackupCode } from "@/lib/auth/amr";
import { getSession, remainingBackupCodes, twoFactorEnabled } from "@/lib/auth/server";
import { actionTotal, DASHBOARD_PATHS, dashboardsFor, type Dashboard, type DashboardKey } from "@/lib/hire/dashboards";
import { resolveViewer } from "@/lib/provisioning";
import { formatRole, type Lookup } from "@/lib/role-label";
import { secondFactorNeed } from "@/lib/viewer-state";

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
  const roles = (await getTranslations("Roles")) as unknown as Lookup;
  const workflows = (await getTranslations("Workflows")) as unknown as Lookup;
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  const need = viewer.status === "ready" ? secondFactorNeed(viewer.me, twoFactorEnabled(session)) : null;
  const backup = session && signedInWithBackupCode(session.session) ? await remainingBackupCodes(session.user.id) : undefined;
  // The dashboards this person's roles call for (brief 5.1), loaded in parallel. One that
  // fails to load is left out with a note; the API checks every permission again.
  const keys: DashboardKey[] =
    viewer.status === "ready" && !need ? dashboardsFor(viewer.me.roles, viewer.me.permissions) : [];
  const loaded = await Promise.all(keys.map((key) => callApi((api) => api.GET(DASHBOARD_PATHS[key]))));
  const boards: { key: DashboardKey; data: Dashboard }[] = [];
  keys.forEach((key, index) => {
    const result = loaded[index];
    if (result?.ok) boards.push({ key, data: result.data });
  });
  const failed = loaded.some((result) => !result.ok);

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
                  {formatRole(roles, workflows, role.role, role.workflow)}
                </Badge>
              ))}
            </div>
          </PageHeader>
          {backup !== undefined && <BackupCodeNotice remaining={backup} />}
          {need && <SecondFactorNeeded need={need} />}
          {boards.length === 0 && (
            <div className="rise" style={{ "--rise-index": 1 } as React.CSSProperties}>
              <ActionNeeded count={0} />
              {!need && keys.length === 0 && <p className="mt-3 text-sm text-muted-foreground">{t("noDashboards")}</p>}
            </div>
          )}
          {failed && (
            <p role="status" className="text-sm text-muted-foreground">
              {t("dashboardsUnavailable")}
            </p>
          )}
          {boards.length > 0 && (
            <p className="sr-only" data-action-total={actionTotal(boards.map((b) => b.data))}>
              {t("actionNeeded")}
            </p>
          )}
          {boards.map((board, index) => (
            <div key={board.key} className="rise" style={{ "--rise-index": index + 1 } as React.CSSProperties}>
              <RoleDashboard dashboard={board.key} data={board.data} />
            </div>
          ))}
        </>
      )}
      {viewer.status === "not_provisioned" && <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />}
      {(viewer.status === "unavailable" || viewer.status === "unauthenticated") && <Unavailable retryHref={`/${locale}`} />}
    </div>
  );
}

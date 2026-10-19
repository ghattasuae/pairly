// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import type * as React from "react";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { HomeSummary, type SummaryTile } from "@/components/home/home-summary";
import { PayRoleDashboard } from "@/components/home/pay-dashboards";
import { RoleDashboard } from "@/components/home/role-dashboards";
import { SystemRoleDashboard } from "@/components/home/system-dashboards";
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
import {
  PAY_DASHBOARD_PATHS,
  payActionTotal,
  payDashboardsFor,
  type PayDashboard,
  type PayDashboardKey,
} from "@/lib/pay/dashboards";
import { resolveViewer } from "@/lib/provisioning";
import {
  quietlyAbsent,
  SYSTEM_DASHBOARD_PATHS,
  systemActionTotal,
  systemDashboardsFor,
  type SystemDashboard,
  type SystemDashboardKey,
} from "@/lib/system-dashboards";
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
  // Pay's dashboards (brief 5.1 items 8 to 10) come with Hire's, by role.
  const payKeys: PayDashboardKey[] =
    viewer.status === "ready" && !need ? payDashboardsFor(viewer.me.roles, viewer.me.permissions) : [];
  // The shared and system boards (brief 5.1 items 12 to 15): auditor, admin, AI
  // governance and, for admins of the system organisation, platform operations.
  const sysKeys: SystemDashboardKey[] =
    viewer.status === "ready" && !need ? systemDashboardsFor(viewer.me.roles, viewer.me.permissions) : [];
  const [loaded, payLoaded, sysLoaded] = await Promise.all([
    Promise.all(keys.map((key) => callApi((api) => api.GET(DASHBOARD_PATHS[key])))),
    Promise.all(payKeys.map((key) => callApi((api) => api.GET(PAY_DASHBOARD_PATHS[key])))),
    Promise.all(sysKeys.map((key) => callApi((api) => api.GET(SYSTEM_DASHBOARD_PATHS[key])))),
  ]);
  const boards: { key: DashboardKey; data: Dashboard }[] = [];
  keys.forEach((key, index) => {
    const result = loaded[index];
    if (result?.ok) boards.push({ key, data: result.data });
  });
  const payBoards: { key: PayDashboardKey; data: PayDashboard }[] = [];
  payKeys.forEach((key, index) => {
    const result = payLoaded[index];
    if (result?.ok) payBoards.push({ key, data: result.data });
  });
  const sysBoards: { key: SystemDashboardKey; data: SystemDashboard }[] = [];
  let sysFailed = false;
  sysKeys.forEach((key, index) => {
    const result = sysLoaded[index];
    if (result?.ok) sysBoards.push({ key, data: result.data });
    else if (result && !quietlyAbsent(key, result.status)) sysFailed = true;
  });
  const failed = loaded.some((result) => !result.ok) || payLoaded.some((result) => !result.ok) || sysFailed;
  const shown = boards.length + payBoards.length + sysBoards.length;
  const total =
    actionTotal(boards.map((b) => b.data)) +
    payActionTotal(payBoards.map((b) => b.data)) +
    systemActionTotal(sysBoards.map((b) => b.data));
  // One stat tile per dashboard, in the order the dashboards follow below.
  const [hireTitles, payTitles, sysTitles] = await Promise.all([
    getTranslations("Dashboards"),
    getTranslations("PayDashboards"),
    getTranslations("SystemDashboards"),
  ]);
  const tiles: SummaryTile[] = [
    ...boards.map((b) => ({
      anchor: `dash-${b.key}`,
      title: hireTitles(`title.${b.key}`),
      count: actionTotal([b.data]),
      lists: b.data.action.length,
    })),
    ...payBoards.map((b) => ({
      anchor: `pay-dash-${b.key}`,
      title: payTitles(`title.${b.key}`),
      count: payActionTotal([b.data]),
      lists: b.data.action.length,
    })),
    ...sysBoards.map((b) => ({
      anchor: `sys-dash-${b.key}`,
      title: sysTitles.has(`title.${b.key}` as "title.auditor") ? sysTitles(`title.${b.key}` as "title.auditor") : b.key.replace(/_/g, " "),
      count: systemActionTotal([b.data]),
      lists: b.data.action.length,
    })),
  ];

  return (
    <div className="page space-y-10">
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
          {shown === 0 && (
            <div className="rise" style={{ "--rise-index": 1 } as React.CSSProperties}>
              <ActionNeeded count={0} />
              {!need && keys.length === 0 && payKeys.length === 0 && sysKeys.length === 0 && <p className="mt-3 text-sm text-muted-foreground">{t("noDashboards")}</p>}
            </div>
          )}
          {failed && (
            <p role="status" className="text-sm text-muted-foreground">
              {t("dashboardsUnavailable")}
            </p>
          )}
          {shown > 0 && <HomeSummary total={total} tiles={tiles} now={new Date()} />}
          {shown > 0 && (
            <p
              className="sr-only"
              data-action-total={total}
            >
              {t("actionNeeded")}
            </p>
          )}
          {boards.map((board, index) => (
            <div key={board.key} className="rise" style={{ "--rise-index": index + 2 } as React.CSSProperties}>
              <RoleDashboard dashboard={board.key} data={board.data} />
            </div>
          ))}
          {payBoards.map((board, index) => (
            <div key={`pay-${board.key}`} className="rise" style={{ "--rise-index": boards.length + index + 2 } as React.CSSProperties}>
              <PayRoleDashboard dashboard={board.key} data={board.data} />
            </div>
          ))}
          {sysBoards.map((board, index) => (
            <div
              key={`sys-${board.key}`}
              className="rise"
              style={{ "--rise-index": boards.length + payBoards.length + index + 2 } as React.CSSProperties}
            >
              <SystemRoleDashboard dashboard={board.key} data={board.data} />
            </div>
          ))}
        </>
      )}
      {viewer.status === "not_provisioned" && <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />}
      {(viewer.status === "unavailable" || viewer.status === "unauthenticated") && <Unavailable retryHref={`/${locale}`} />}
    </div>
  );
}

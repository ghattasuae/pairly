// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { HireReportLinks, JobsList, RediscoverySearch, Referrals, WaitingCard } from "@/components/hire/hire-home";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { isNavVisible } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

import { sectionMetadata } from "../section-placeholder";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "hire");
}

/**
 * Hire home, action first (brief 5.3): who has waited longest for an update, then the
 * jobs and their pipelines, finding past candidates, and referrals. Each part shows only
 * for people whose permissions allow it (the API checks again on every call).
 */
export default async function HirePage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="page">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  if (viewer.status !== "ready") {
    return (
      <div className="page">
        <Unavailable retryHref={`/${locale}/hire`} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!isNavVisible("hire", permissions)) notFound();
  const team = permissions.includes("hire.candidate.read");
  const refers = permissions.includes("hire.referral.create");
  const t = await getTranslations("Hire");
  const nav = await getTranslations("Nav");
  const [waiting, jobs, referrals] = await Promise.all([
    team ? callApi((api) => api.GET("/v1/hire/pipeline/waiting", { params: { query: { limit: 6 } } })) : null,
    callApi((api) => api.GET("/v1/hire/jobs", { params: { query: { limit: 100 } } })),
    refers ? callApi((api) => api.GET("/v1/hire/referrals/mine")) : null,
  ]);
  if (!jobs.ok) {
    return (
      <div className="page">
        <Unavailable retryHref={`/${locale}/hire`} />
      </div>
    );
  }
  const working = jobs.data.items.filter((j) => j.status !== "closed");
  const open = jobs.data.items.filter((j) => j.status === "open");
  return (
    <div className="page space-y-8">
      <PageHeader title={nav("hire")}>
        <p>{t("home.intro")}</p>
      </PageHeader>
      {team && waiting?.ok && <WaitingCard waiting={waiting.data} />}
      {permissions.includes("hire.requisition.read") && (
        <p className="text-sm">
          <Link href="/hire/requisitions" className="text-primary hover:underline" data-requisitions-link>
            {t("home.requisitions")}
          </Link>
        </p>
      )}
      <HireReportLinks permissions={permissions} labels={{
        analytics: t("home.analytics"),
        bias: t("home.bias"),
        surveys: t("home.surveys"),
        calibration: t("home.calibration"),
      }} />
      {team && <JobsList jobs={working} canEdit={permissions.includes("hire.job.write")} />}
      {team && <RediscoverySearch />}
      {refers && <Referrals jobs={open} referrals={referrals?.ok ? referrals.data : []} />}
    </div>
  );
}

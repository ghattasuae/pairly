// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { HiringAnalytics } from "@/components/hire/hiring-analytics";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { filterApplyClass, filterFieldClass } from "@/components/ui/filter-bar";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { analyticsQuery } from "@/lib/hire/analytics";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "HireAnalytics" });
  return { title: t("title") };
}

/**
 * Hiring analytics for HR and talent leads and admins (`hire.analytics.read`, brief 3.10).
 * Filters live in the URL (a plain GET form), so a view can be shared and reloaded.
 */
export default async function HiringAnalyticsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session, raw] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="page">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/analytics`;
  if (viewer.status !== "ready") {
    return (
      <div className="page">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("hire.analytics.read")) notFound();
  const query = analyticsQuery(raw);
  const [result, jobs] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/analytics", { params: { query } })),
    callApi((api) => api.GET("/v1/hire/jobs", { params: { query: { limit: 100 } } })),
  ]);
  if (!result.ok) {
    return (
      <div className="page">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("HireAnalytics");
  const field = filterFieldClass;
  return (
    <div className="page space-y-8">
      <PageHeader title={t("title")}>
        <p>{t("intro")}</p>
      </PageHeader>
      <form method="get" className="filter-bar" aria-label={t("filters")}>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("job")}
          <select name="job_id" defaultValue={query.job_id ?? ""} className={field}>
            <option value="">{t("allJobs")}</option>
            {(jobs.ok ? jobs.data.items : []).map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("since")}
          <input type="date" name="since" defaultValue={query.since ?? ""} className={field} />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("until")}
          <input type="date" name="until" defaultValue={query.until ?? ""} className={field} />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("ghostDays")}
          <input
            type="number"
            name="ghost_days"
            min={1}
            max={365}
            defaultValue={result.data.settings.ghost_days}
            className={`${field} w-24`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("threshold")}
          <input
            type="number"
            name="agreement_threshold"
            min={0}
            max={100}
            defaultValue={result.data.settings.agreement_threshold}
            className={`${field} w-24`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          {t("minutes")}
          <input
            type="number"
            name="minutes_per_booking"
            min={0}
            max={240}
            defaultValue={result.data.settings.minutes_per_booking}
            className={`${field} w-24`}
          />
        </label>
        <button type="submit" className={filterApplyClass}>
          {t("apply")}
        </button>
      </form>
      <HiringAnalytics data={result.data} />
    </div>
  );
}

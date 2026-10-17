// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { SurveyResults } from "@/components/hire/survey-results";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
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
  const t = await getTranslations({ locale, namespace: "HireSurveyResults" });
  return { title: t("title") };
}

/**
 * Candidate experience survey results (`hire.survey.read`): totals with small groups
 * hidden; answers with a name attached only for people who also read candidates.
 */
export default async function SurveyResultsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session, raw] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/surveys`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.survey.read")) notFound();
  const filters = analyticsQuery({ job_id: raw.job_id, since: raw.since });
  const query = { ...(filters.job_id ? { job_id: filters.job_id } : {}), ...(filters.since ? { since: filters.since } : {}) };
  const seesNamed = permissions.includes("hire.candidate.read");
  const [results, named, jobs] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/surveys/results", { params: { query } })),
    seesNamed
      ? callApi((api) =>
          api.GET("/v1/hire/surveys/named", { params: { query: filters.job_id ? { job_id: filters.job_id } : {} } }),
        )
      : null,
    callApi((api) => api.GET("/v1/hire/jobs", { params: { query: { limit: 100 } } })),
  ]);
  if (!results.ok) {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("HireSurveyResults");
  const field = "rounded-md border bg-background px-2 py-1.5 text-sm";
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={t("title")}>
        <p>{t("intro", { min: results.data.min_group })}</p>
      </PageHeader>
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm" aria-label={t("filters")}>
        <label className="flex flex-col gap-1">
          {t("job")}
          <select name="job_id" defaultValue={filters.job_id ?? ""} className={field}>
            <option value="">{t("allJobs")}</option>
            {(jobs.ok ? jobs.data.items : []).map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          {t("since")}
          <input type="date" name="since" defaultValue={filters.since ?? ""} className={field} />
        </label>
        <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground">
          {t("apply")}
        </button>
      </form>
      <SurveyResults results={results.data} named={named?.ok ? named.data : null} />
    </div>
  );
}

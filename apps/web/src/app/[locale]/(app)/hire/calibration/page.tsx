// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { CalibrationLabeller, CalibrationReportView } from "@/components/hire/calibration-workspace";
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
  const t = await getTranslations({ locale, namespace: "HireCalibration" });
  return { title: t("title") };
}

/**
 * Calibration mode (brief 3.3.11): people with `hire.calibration.write` label a sample;
 * people with `hire.analytics.read` read the agreement report for the chosen job.
 */
export default async function CalibrationPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session, raw] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/calibration`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  const canLabel = permissions.includes("hire.calibration.write");
  const canReport = permissions.includes("hire.analytics.read");
  if (!canLabel && !canReport) notFound();
  const jobId = analyticsQuery({ job_id: raw.job_id }).job_id ?? null;
  const [jobs, sample, report] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/jobs", { params: { query: { limit: 100 } } })),
    jobId && canLabel
      ? callApi((api) => api.GET("/v1/hire/calibration/sample", { params: { query: { job_id: jobId, size: 10 } } }))
      : null,
    jobId && canReport
      ? callApi((api) => api.GET("/v1/hire/calibration/report", { params: { query: { job_id: jobId } } }))
      : null,
  ]);
  if (!jobs.ok || sample?.ok === false || report?.ok === false) {
    return (
      <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("HireCalibration");
  return (
    <div className="mx-auto w-full max-w-6xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={t("title")}>
        <p>{t("intro")}</p>
      </PageHeader>
      <form method="get" className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">
          {t("job")}
          <select name="job_id" defaultValue={jobId ?? ""} className="rounded-md border bg-background px-2 py-1.5">
            <option value="" disabled>
              {t("chooseJob")}
            </option>
            {jobs.data.items.map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground">
          {t("open")}
        </button>
      </form>
      {!jobId && <p className="text-sm text-muted-foreground">{t("chooseJob")}</p>}
      {jobId && report?.ok && (
        <section className="space-y-3">
          <h2 className="font-display text-xl">{t("reportTitle")}</h2>
          <CalibrationReportView report={report.data} jobId={jobId} />
        </section>
      )}
      {jobId && sample?.ok && (
        <section className="space-y-3">
          <h2 className="font-display text-xl">{t("labelTitle")}</h2>
          <CalibrationLabeller sample={sample.data} />
        </section>
      )}
    </div>
  );
}

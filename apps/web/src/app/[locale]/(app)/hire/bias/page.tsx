// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { BiasReport } from "@/components/hire/bias-report";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { biasExportQuery, biasQuery } from "@/lib/hire/bias";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

const STAGES = ["applied", "screening", "shortlist", "interview", "offer", "hired"] as const;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "HireBias" });
  return { title: t("title") };
}

/**
 * The bias and adverse-impact report (`hire.bias.read`, brief 3.10). Viewing it is audited
 * by the API; the export link appears for people with `hire.bias.export`.
 */
export default async function BiasPage({ params, searchParams }: Props) {
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
  const retry = `/${locale}/hire/bias`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.bias.read")) notFound();
  const query = biasQuery(raw);
  const [result, jobs] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/bias", { params: { query } })),
    callApi((api) => api.GET("/v1/hire/jobs", { params: { query: { limit: 100 } } })),
  ]);
  if (!result.ok) {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("HireBias");
  const a = await getTranslations("HireAnalytics");
  const stageLabels = Object.fromEntries(STAGES.map((s) => [s, t(`stages.${s}`)]));
  const search = new URLSearchParams(Object.entries(query).map(([k, v]) => [k, String(v)]));
  const exportHref = permissions.includes("hire.bias.export")
    ? `/api/hire/bias/export${biasExportQuery(search)}`
    : null;
  const field = "rounded-md border bg-background px-2 py-1.5 text-sm";
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={t("title")}>
        <p>{t("intro")}</p>
      </PageHeader>
      <form method="get" className="flex flex-wrap items-end gap-3 rounded-lg border p-4 text-sm" aria-label={a("filters")}>
        <label className="flex flex-col gap-1">
          {a("job")}
          <select name="job_id" defaultValue={query.job_id ?? ""} className={field}>
            <option value="">{a("allJobs")}</option>
            {(jobs.ok ? jobs.data.items : []).map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          {a("since")}
          <input type="date" name="since" defaultValue={query.since ?? ""} className={field} />
        </label>
        <label className="flex flex-col gap-1">
          {a("until")}
          <input type="date" name="until" defaultValue={query.until ?? ""} className={field} />
        </label>
        <label className="flex flex-col gap-1">
          {t("minSample")}
          <input
            type="number"
            name="min_sample"
            min={5}
            max={1000}
            defaultValue={result.data.settings.min_sample}
            className={`${field} w-24`}
          />
        </label>
        <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground">
          {a("apply")}
        </button>
      </form>
      <BiasReport report={result.data} exportHref={exportHref} stageLabels={stageLabels} />
    </div>
  );
}

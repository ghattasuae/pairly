// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { OpsJobRuns } from "@/components/home/ops-job-runs";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

const JOB_RE = /^[a-z][a-z0-9_]{0,63}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "OpsJobs" });
  return { title: t("title") };
}

/**
 * Recent background job runs (`GET /v1/ops/jobs`), linked from the platform operations
 * board. Only admins of the system organisation reach it; anyone else gets 404, as from
 * the API.
 */
export default async function OpsJobsPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, query] = await Promise.all([resolveViewer(), searchParams]);
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/ops/jobs`} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("core.platform.read")) notFound();
  const raw = typeof query.job === "string" ? query.job : "";
  const job = JOB_RE.test(raw) ? raw : null;
  const runs = await callApi((api) =>
    api.GET("/v1/ops/jobs", { params: { query: { limit: 100, ...(job ? { job_name: job } : {}) } } }),
  );
  if (!runs.ok && runs.status === 404) notFound();
  const t = await getTranslations("OpsJobs");
  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 px-5 py-10 sm:px-8">
      <PageHeader eyebrow={job ?? t("all")} title={t("title")}>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </PageHeader>
      {runs.ok ? <OpsJobRuns runs={runs.data.items} /> : <Unavailable retryHref={`/${locale}/ops/jobs`} />}
    </div>
  );
}

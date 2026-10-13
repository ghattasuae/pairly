// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InterviewPlanEditor } from "@/components/hire/interview-plan";
import { JobEditor } from "@/components/hire/job-editor";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; jobId: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "HireJob" });
  return { title: t("editTitle") };
}

/** Edit one job, its rubric, lock and publish (brief 5.3). */
export default async function EditJobPage({ params }: Props) {
  const { locale: raw, jobId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(jobId)) notFound();
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/jobs/${jobId}`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.job.write")) notFound();
  const canRubric = permissions.includes("hire.rubric.read") && permissions.includes("hire.rubric.write");
  const path = { params: { path: { job_id: jobId } } };
  const [job, rubric, checks, plan] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}", path)),
    canRubric ? callApi((api) => api.GET("/v1/hire/jobs/{job_id}/rubric", path)) : null,
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}/checks", path)),
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}/interview-plan", path)),
  ]);
  if (!job.ok) {
    if (job.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("HireJob");
  return (
    <div className="mx-auto w-full max-w-4xl space-y-8 px-5 py-10 sm:px-8">
      <Link href="/hire" className="text-sm text-primary hover:underline">
        {t("back")}
      </Link>
      <PageHeader title={t("editTitle")}>
        <p>{t("editIntro")}</p>
      </PageHeader>
      <JobEditor
        key={job.data.id}
        job={job.data}
        rubric={rubric?.ok ? rubric.data : null}
        checks={checks.ok ? checks.data : null}
        canRubric={canRubric}
        canLock={permissions.includes("hire.rubric.lock")}
        canPublish={permissions.includes("hire.job.publish")}
        totpEnabled={twoFactorEnabled(session)}
      />
      {plan.ok && <InterviewPlanEditor key={`plan-${plan.data.version ?? 0}`} jobId={job.data.id} initial={plan.data} />}
    </div>
  );
}

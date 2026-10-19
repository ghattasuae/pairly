// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ChecklistPanel } from "@/components/hire/checklist-panel";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; applicationId: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Checklist" });
  return { title: t("title") };
}

/** The pre-employment checklist for one candidate (brief 3.8.4), HR and admins only. */
export default async function ChecklistPage({ params }: Props) {
  const { locale: raw, applicationId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(applicationId)) notFound();
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="page max-w-3xl">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/applications/${applicationId}/checklist`;
  if (viewer.status !== "ready") {
    return (
      <div className="page max-w-3xl">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("hire.checklist.read")) notFound();
  const checklist = await callApi((api) =>
    api.GET("/v1/hire/applications/{application_id}/checklist", { params: { path: { application_id: applicationId } } }),
  );
  if (!checklist.ok) {
    if (checklist.code === "not_found") notFound();
    return (
      <div className="page max-w-3xl">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const config = await callApi((api) => api.GET("/v1/hire/jobs/{job_id}/checklist", { params: { path: { job_id: checklist.data.job_id } } }));
  if (!config.ok) {
    return (
      <div className="page max-w-3xl">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  return (
    <div className="page max-w-3xl">
      <ChecklistPanel initial={checklist.data} config={config.data} />
    </div>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InterviewKit } from "@/components/hire/interview-kit";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; applicationId: string }>;
  searchParams: Promise<{ stage?: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STAGE_RE = /^[a-z][a-z0-9_]{0,31}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "InterviewKit" });
  return { title: t("title") };
}

/** A candidate's interview kit for a stage, for the hiring team, phone first. */
export default async function KitPage({ params, searchParams }: Props) {
  const { locale: raw, applicationId } = await params;
  const { stage: rawStage } = await searchParams;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(applicationId)) notFound();
  const stage = rawStage && STAGE_RE.test(rawStage) ? rawStage : "interview";
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="page max-w-2xl">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/applications/${applicationId}/kit?stage=${stage}`;
  if (viewer.status !== "ready") {
    return (
      <div className="page max-w-2xl">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("hire.interview.read")) notFound();
  const kit = await callApi((api) =>
    api.GET("/v1/hire/applications/{application_id}/interview-kit", {
      params: { path: { application_id: applicationId }, query: { stage, locale } },
    }),
  );
  if (!kit.ok) {
    if (kit.code === "not_found") notFound();
    return (
      <div className="page max-w-2xl">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  return (
    <div className="page max-w-2xl space-y-4">
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">
          {kit.data.job_title} · {kit.data.stage_name}
        </p>
        <h1 className="font-display text-2xl">{kit.data.candidate_label}</h1>
      </header>
      <InterviewKit kit={kit.data} />
    </div>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { DebriefView } from "@/components/hire/debrief";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; jobId: string; applicationId: string }>;
  searchParams: Promise<{ stage?: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STAGE_RE = /^[a-z][a-z0-9_]{0,31}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Debrief" });
  return { title: t("title") };
}

/** The hiring manager's debrief for one candidate and stage (brief 3.7.4, 5.3). */
export default async function DebriefPage({ params, searchParams }: Props) {
  const { locale: raw, jobId, applicationId } = await params;
  const { stage: rawStage } = await searchParams;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(jobId) || !UUID_RE.test(applicationId)) notFound();
  const stage = rawStage && STAGE_RE.test(rawStage) ? rawStage : "interview";
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/jobs/${jobId}/debrief/${applicationId}`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("hire.scorecard.read")) notFound();
  const debrief = await callApi((api) =>
    api.GET("/v1/hire/applications/{application_id}/debrief", {
      params: { path: { application_id: applicationId }, query: { stage, locale } },
    }),
  );
  const t = await getTranslations("Debrief");
  if (!debrief.ok) {
    if (debrief.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-8">
        {debrief.code === "permission_denied" ? <p role="alert">{t("submitFirst")}</p> : <Unavailable retryHref={retry} />}
      </div>
    );
  }
  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-8">
      <Link href={`/hire/jobs/${jobId}/shortlist`} className="text-sm text-primary hover:underline">
        {t("back")}
      </Link>
      <DebriefView initial={debrief.data} totpEnabled={twoFactorEnabled(session)} />
    </div>
  );
}

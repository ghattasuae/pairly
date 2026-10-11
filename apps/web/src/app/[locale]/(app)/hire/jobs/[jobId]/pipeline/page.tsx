// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { JobRecruiters } from "@/components/hire/job-recruiters";
import { PipelineBoard } from "@/components/hire/pipeline-board";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { parseBoardState } from "@/lib/hire/board";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; jobId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Hire" });
  return { title: t("pipeline") };
}

/**
 * One job's pipeline: the board, and the open candidate (from the URL) rendered on the
 * server so a reload or a shared link opens exactly where the person was.
 */
export default async function PipelinePage({ params, searchParams }: Props) {
  const { locale: raw, jobId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(jobId)) notFound();
  const [viewer, session, query] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/jobs/${jobId}/pipeline`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.candidate.read")) notFound();
  const state = parseBoardState(query);
  const tShortlist = await getTranslations({ locale, namespace: "Shortlist" });
  const [board, profile, templates, team, job] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}/pipeline", { params: { path: { job_id: jobId } } })),
    state.candidate
      ? callApi((api) =>
          api.GET("/v1/hire/pipeline/applications/{application_id}", {
            params: { path: { application_id: state.candidate ?? "" } },
          }),
        )
      : Promise.resolve(null),
    callApi((api) => api.GET("/v1/hire/messages/templates", { params: { query: { locale } } })),
    callApi((api) => api.GET("/v1/hire/pipeline/team")),
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}", { params: { path: { job_id: jobId } } })),
  ]);
  if (!board.ok) {
    if (board.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  return (
    <>
      <div className="mx-auto flex w-full max-w-7xl flex-wrap items-start justify-between gap-3 px-5 pt-4 sm:px-8">
        <div className="min-w-0 flex-1">
          {job.ok && (
            <JobRecruiters
              job={job.data}
              team={team.ok ? team.data : []}
              canEdit={permissions.includes("hire.job.write")}
            />
          )}
        </div>
        <Link
          href={`/hire/jobs/${jobId}/shortlist`}
          className="text-sm text-primary hover:underline"
          data-shortlist-link
        >
          {tShortlist("title")}
        </Link>
      </div>
      <PipelineBoard
        board={board.data}
        initialState={state}
        initialProfile={profile?.ok ? profile.data : null}
        templates={templates.ok ? templates.data : []}
        team={team.ok ? team.data : []}
        canMove={permissions.includes("hire.candidate.decide")}
        canNote={permissions.includes("hire.note.write")}
        canMessage={permissions.includes("hire.message.create")}
        canSchedule={permissions.includes("hire.interview.schedule")}
        totpEnabled={twoFactorEnabled(session)}
      />
    </>
  );
}

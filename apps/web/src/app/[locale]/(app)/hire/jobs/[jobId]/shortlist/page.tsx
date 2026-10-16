// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ShortlistReview } from "@/components/hire/shortlist-review";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { interviewsByApplication, SHORTLIST_STAGE, type ShortlistCandidate } from "@/lib/hire/shortlist";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; jobId: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// One screen: a shortlist is a handful of people; more than this belongs on the board.
const MAX_CANDIDATES = 30;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Shortlist" });
  return { title: t("title") };
}

/**
 * The hiring manager's shortlist review for one job (brief 5.3): every candidate in the
 * shortlist stage with their summary, scores, evidence and interview status, loaded on
 * the server in parallel, and a decision each (advance, hold, reject draft).
 */
export default async function ShortlistPage({ params }: Props) {
  const { locale: raw, jobId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(jobId)) notFound();
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/jobs/${jobId}/shortlist`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.candidate.read")) notFound();
  const canScreening = permissions.includes("hire.screening.read");
  const canInterviews = permissions.includes("hire.interview.read");
  const [board, interviews] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/jobs/{job_id}/pipeline", { params: { path: { job_id: jobId } } })),
    canInterviews
      ? callApi((api) =>
          api.GET("/v1/hire/scheduling/interviews", { params: { query: { job_id: jobId, limit: 500 } } }),
        )
      : Promise.resolve(null),
  ]);
  if (!board.ok) {
    if (board.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const byApplication = interviewsByApplication(interviews?.ok ? interviews.data : []);
  const cards = board.data.cards.filter((c) => c.stage_key === SHORTLIST_STAGE).slice(0, MAX_CANDIDATES);
  const candidates: ShortlistCandidate[] = await Promise.all(
    cards.map(async (card) => {
      const path = { params: { path: { application_id: card.application_id } } };
      const [profile, screening] = await Promise.all([
        callApi((api) => api.GET("/v1/hire/pipeline/applications/{application_id}", path)),
        canScreening
          ? callApi((api) => api.GET("/v1/hire/applications/{application_id}/screening", path))
          : Promise.resolve(null),
      ]);
      return {
        card,
        profile: profile.ok ? profile.data : null,
        screening: screening?.ok ? screening.data : null,
        interviews: byApplication.get(card.application_id) ?? [],
      };
    }),
  );
  return (
    <ShortlistReview
      jobId={jobId}
      jobTitle={board.data.job_title}
      stages={board.data.stages}
      candidates={candidates}
      canDecide={permissions.includes("hire.candidate.decide")}
      canNote={permissions.includes("hire.note.write")}
      canMessage={permissions.includes("hire.message.create")}
      totpEnabled={twoFactorEnabled(session)}
    />
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ScreeningMode } from "@/components/hire/screening-mode";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { loadCandidate } from "@/lib/actions/screening";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { parseScreenState } from "@/lib/hire/screening";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; jobId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Screen" });
  return { title: t("title") };
}

/**
 * Recruiter screening mode for one job (brief 5.3): the queue, the open candidate's CV and
 * scores, rendered on the server for the candidate in the URL (or the first one), so a
 * reload or a shared link opens exactly where the person was. Desktop first, usable on a
 * tablet.
 */
export default async function ScreenPage({ params, searchParams }: Props) {
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
  const retry = `/${locale}/hire/jobs/${jobId}/screen`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.candidate.read") || !permissions.includes("hire.screening.read")) notFound();
  const state = parseScreenState(query);
  const queue = await callApi((api) =>
    api.GET("/v1/hire/jobs/{job_id}/screening-queue", { params: { path: { job_id: jobId } } }),
  );
  if (!queue.ok) {
    if (queue.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const known = state.candidate && queue.data.items.some((i) => i.application_id === state.candidate);
  const candidate = known ? state.candidate : (queue.data.items[0]?.application_id ?? null);
  const detail = candidate ? await loadCandidate(candidate) : null;
  return (
    <ScreeningMode
      queue={queue.data}
      initialState={{ candidate, q: state.q }}
      initialDetail={detail?.ok ? detail.detail : null}
      canMove={permissions.includes("hire.candidate.decide")}
      canNote={permissions.includes("hire.note.write")}
      canMessage={permissions.includes("hire.message.create")}
      canOverride={permissions.includes("hire.screening.override")}
      totpEnabled={twoFactorEnabled(session)}
    />
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { DecisionView } from "@/components/decisions/decision-view";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import type { Appeal, Replay } from "@/lib/api/types";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { isDecisionId } from "@/lib/decision-id";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; id: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Decision" });
  return { title: t("title") };
}

/**
 * A decision record as a readable page (brief 2.3): the receipt the API redacts for this
 * person's role, its replays and its appeals. Buttons show only what the person's
 * permissions suggest they can do; the API checks every call.
 */
export default async function DecisionPage({ params }: Props) {
  const { locale: raw, id } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!isDecisionId(id)) notFound();
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/decisions/${id}`} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;

  const [receipt, replays, openAppeals, resolvedAppeals] = await Promise.all([
    callApi((api) => api.GET("/v1/decisions/{decision_id}/receipt", { params: { path: { decision_id: id } } })),
    callApi((api) => api.GET("/v1/decisions/{decision_id}/replays", { params: { path: { decision_id: id } } })),
    callApi((api) => api.GET("/v1/appeals", { params: { query: { status: "open", decision_id: id, limit: 200 } } })),
    callApi((api) => api.GET("/v1/appeals", { params: { query: { status: "resolved", decision_id: id, limit: 200 } } })),
  ]);
  if (!receipt.ok) {
    if (["not_found", "permission_denied", "invalid_input"].includes(receipt.code)) notFound();
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/decisions/${id}`} />
      </div>
    );
  }
  const data = receipt.data as Record<string, unknown>;
  const workflow = typeof data.workflow === "string" ? data.workflow : "";
  // The API filters by decision; the check here only guards against a wrong answer.
  const appeals: Appeal[] | null =
    openAppeals.ok || resolvedAppeals.ok
      ? [...(openAppeals.ok ? openAppeals.data : []), ...(resolvedAppeals.ok ? resolvedAppeals.data : [])].filter(
          (appeal) => appeal.decision_id === id,
        )
      : null;
  const replayList: Replay[] = replays.ok ? replays.data : [];

  return (
    <DecisionView
      decisionId={id}
      receipt={data}
      replays={replayList}
      appeals={appeals}
      can={{
        replay: permissions.includes("core.decision.replay"),
        fileAppeal: permissions.includes("core.appeal.create"),
        resolveAppeal:
          permissions.includes("core.appeal.decide") ||
          permissions.some((p) => p.startsWith(`${workflow}.`) && (p.endsWith(".approve") || p.endsWith(".decide"))),
      }}
      totpEnabled={twoFactorEnabled(session)}
    />
  );
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getFormatter, getTranslations, setRequestLocale } from "next-intl/server";

import { JsonView } from "@/components/data/json-view";
import { PortalSignIn } from "@/components/portal/portal-sign-in";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { portalCall, readPortalToken } from "@/lib/portal/session";

interface Props {
  params: Promise<{ locale: string; decisionId: string }>;
}

const DECISION = /^dec_[0-9A-HJKMNP-TV-Z]{26}$/;
const KINDS = new Set(["score", "extraction", "shortlist_suggestion", "human_override", "appeal_outcome"]);

type Receipt = {
  decision_id: string;
  kind: string;
  decided_at: string | null;
  outcome?: Record<string, unknown>;
  policy_versions?: { name?: string; version?: number }[];
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Portal" });
  return { title: t("privacy.receiptTitle"), referrer: "no-referrer", robots: { index: false } };
}

/**
 * `/<locale>/portal/privacy/decisions/<id>`: the candidate-facing receipt of one decision
 * about their application (the API's subject receipt: no staff names, no model details).
 */
export default async function PortalReceiptPage({ params }: Props) {
  const { locale: raw, decisionId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!DECISION.test(decisionId)) notFound();
  const token = await readPortalToken();
  if (token === null) return <PortalSignIn org={null} />;
  const result = await portalCall<Receipt>(`/v1/hire/portal/privacy/decisions/${decisionId}`);
  if (!result.ok) {
    if (result.code === "not_found") notFound();
    if (result.code === "not_signed_in") return <PortalSignIn org={token.split(".")[1] ?? null} expired />;
    return <Unavailable retryHref={`/${locale}/portal/privacy/decisions/${decisionId}`} />;
  }
  const t = await getTranslations({ locale, namespace: "Portal" });
  const format = await getFormatter({ locale });
  const receipt = result.data;
  return (
    <article className="space-y-6" data-receipt={receipt.decision_id}>
      <Link href="/portal/privacy" className="text-sm text-primary underline-offset-4 hover:underline">
        {t("privacy.title")}
      </Link>
      <header className="space-y-2">
        <h1 className="font-display text-3xl leading-tight">{t("privacy.receiptTitle")}</h1>
        <p className="text-muted-foreground">{t("privacy.receiptIntro")}</p>
        {receipt.decided_at && (
          <p className="text-sm">
            {t("privacy.decidedAt", { date: format.dateTime(new Date(receipt.decided_at), { dateStyle: "medium" }) })}
          </p>
        )}
      </header>
      <dl className="grid gap-3 rounded-2xl border bg-surface p-5 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-muted-foreground">{t("privacy.decisionId")}</dt>
        <dd className="font-mono text-xs" dir="ltr">
          {receipt.decision_id}
        </dd>
        <dt className="text-muted-foreground">{t("privacy.decisionsTitle")}</dt>
        <dd>{KINDS.has(receipt.kind) ? t(`privacy.kind.${receipt.kind}` as "privacy.kind.score") : receipt.kind}</dd>
        {receipt.policy_versions && receipt.policy_versions.length > 0 && (
          <>
            <dt className="text-muted-foreground">{t("privacy.policies")}</dt>
            <dd>
              {receipt.policy_versions.map((p) => `${p.name ?? ""} v${p.version ?? ""}`).join(", ")}
            </dd>
          </>
        )}
      </dl>
      {receipt.outcome && (
        <section aria-labelledby="receipt-outcome" className="space-y-2">
          <h2 id="receipt-outcome" className="font-display text-xl">
            {t("privacy.outcome")}
          </h2>
          <div className="rounded-xl border bg-surface p-4">
            <JsonView value={receipt.outcome} />
          </div>
        </section>
      )}
    </article>
  );
}

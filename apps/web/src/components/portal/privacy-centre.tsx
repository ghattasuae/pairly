// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Download, Loader2, ShieldCheck, Trash2, UserRoundSearch } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import { requestDeletion, requestHumanReview } from "@/lib/actions/portal";
import type { PortalDecision, PortalPrivacy } from "@/lib/portal/steps";

const KINDS = new Set(["score", "extraction", "shortlist_suggestion", "human_override", "appeal_outcome"]);

/**
 * The candidate's privacy centre (brief 3.9.7): download their own data, ask the admins to
 * delete it (a request a person handles, never automatic), and ask a person to review any
 * AI-assisted step by its decision id, with a receipt for each step.
 */
export function PrivacyCentre({
  privacy: initial,
  deletion = requestDeletion,
  review = requestHumanReview,
}: {
  privacy: PortalPrivacy;
  deletion?: typeof requestDeletion;
  review?: typeof requestHumanReview;
}) {
  const t = useTranslations("Portal");
  const format = useFormatter();
  const ids = useId();
  const [privacy, setPrivacy] = useState(initial);
  const [note, setNote] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState(false);
  const openRequest = privacy.data_requests.some((r) => r.status === "open");

  const askDeletion = async () => {
    setDeleting(true);
    setError(null);
    const result = await deletion(note);
    setDeleting(false);
    if (!result.ok) return setError(t("errors.generic"));
    setNotice(result.created ? t("privacy.deleteSent") : t("privacy.deleteAlready"));
    setNote("");
    setPrivacy((p) => ({
      ...p,
      data_requests: openRequest
        ? p.data_requests
        : [
            { id: "pending", kind: "deletion", status: "open", created_at: new Date().toISOString(), handled_at: null },
            ...p.data_requests,
          ],
    }));
  };

  const askReview = async (decision: PortalDecision) => {
    if (reason.trim().length < 10) return;
    setSending(true);
    setError(null);
    const result = await review(decision.decision_id, reason.trim());
    setSending(false);
    if (!result.ok) return setError(t(`errors.${result.code === "conflict" ? "conflict" : "generic"}`));
    setPrivacy((p) => ({
      ...p,
      decisions: p.decisions.map((d) => (d.decision_id === decision.decision_id ? { ...d, appealed: true, can_appeal: false } : d)),
      appeals: [result.appeal, ...p.appeals],
    }));
    setReviewing(null);
    setReason("");
    setNotice(t("privacy.reviewSent"));
  };

  return (
    <div className="space-y-10" data-privacy-centre>
      <header className="space-y-2">
        <p className="eyebrow">{t("eyebrow")}</p>
        <h1 className="font-display text-[2rem] leading-[1.1] font-normal text-balance sm:text-[2.6rem]">{t("privacy.title")}</h1>
        <p className="text-muted-foreground">{t("privacy.intro")}</p>
      </header>
      <p role="status" aria-live="polite" className="text-sm font-medium" data-portal-notice>
        {notice ?? ""}
      </p>
      {error && (
        <p role="alert" className="text-sm text-danger-ink">
          {error}
        </p>
      )}

      <section aria-labelledby={`${ids}-download`} className="space-y-2 card-hairline p-5">
        <h2 id={`${ids}-download`} className="flex items-center gap-2 font-display text-xl">
          <Download className="size-5" aria-hidden />
          {t("privacy.download")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("privacy.downloadHelp")}</p>
        <a
          href="/api/portal/export"
          download
          className={buttonVariants({ variant: "outline" })}
          data-export
        >
          <Download className="size-4" aria-hidden />
          {t("privacy.download")}
        </a>
      </section>

      <section aria-labelledby={`${ids}-decisions`} className="space-y-3">
        <h2 id={`${ids}-decisions`} className="flex items-center gap-2 font-display text-xl">
          <UserRoundSearch className="size-5" aria-hidden />
          {t("privacy.decisionsTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("privacy.decisionsHelp")}</p>
        {privacy.decisions.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("privacy.noDecisions")}</p>
        ) : (
          <ul className="space-y-3">
            {privacy.decisions.map((d) => (
              <li key={d.decision_id} className="space-y-2 card-hairline p-4" data-decision={d.decision_id}>
                <p className="font-medium">
                  {KINDS.has(d.kind) ? t(`privacy.kind.${d.kind}` as "privacy.kind.score") : d.kind}
                </p>
                <p className="text-xs text-muted-foreground">
                  {d.job_title} · <span dir="ltr">{d.reference}</span> ·{" "}
                  {format.dateTime(new Date(d.decided_at), { dateStyle: "medium" })}
                </p>
                <p className="font-mono text-xs" dir="ltr">
                  {d.decision_id}
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  <Link href={`/portal/privacy/decisions/${d.decision_id}`} className="text-sm text-primary underline-offset-4 hover:underline">
                    {t("privacy.receipt")}
                  </Link>
                  {d.appealed && <Badge>{t("privacy.reviewed")}</Badge>}
                  {d.can_appeal && reviewing !== d.decision_id && (
                    <Button type="button" size="sm" variant="outline" onClick={() => setReviewing(d.decision_id)}>
                      {t("privacy.review")}
                    </Button>
                  )}
                </div>
                {reviewing === d.decision_id && (
                  <form
                    className="space-y-2"
                    aria-label={t("privacy.reviewTitle")}
                    onSubmit={(e) => {
                      e.preventDefault();
                      void askReview(d);
                    }}
                  >
                    <label htmlFor={`${ids}-${d.decision_id}`} className="block text-sm font-medium">
                      {t("privacy.reviewReason")}
                    </label>
                    <Textarea
                      id={`${ids}-${d.decision_id}`}
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      rows={3}
                      minLength={10}
                      maxLength={4000}
                      required
                    />
                    <Button type="submit" size="sm" disabled={sending || reason.trim().length < 10}>
                      {sending && <Loader2 className="animate-spin" aria-hidden />}
                      {t("privacy.reviewSend")}
                    </Button>
                  </form>
                )}
              </li>
            ))}
          </ul>
        )}
        {privacy.appeals.length > 0 && (
          <div className="space-y-2">
            <h3 className="font-medium">{t("privacy.appealsTitle")}</h3>
            <ul className="space-y-1 text-sm">
              {privacy.appeals.map((a) => (
                <li key={a.id} className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs" dir="ltr">
                    {a.decision_id}
                  </span>
                  <Badge variant={a.status === "open" ? "signal" : "default"}>
                    {t(a.status === "open" ? "privacy.appealStatus.open" : "privacy.appealStatus.resolved")}
                  </Badge>
                  {a.outcome_text && <span>{a.outcome_text}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section aria-labelledby={`${ids}-delete`} className="space-y-3 card-hairline p-5">
        <h2 id={`${ids}-delete`} className="flex items-center gap-2 font-display text-xl">
          <Trash2 className="size-5" aria-hidden />
          {t("privacy.deleteTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("privacy.deleteHelp")}</p>
        {openRequest ? (
          <p className="flex items-center gap-2 text-sm">
            <ShieldCheck className="size-4 text-primary" aria-hidden />
            {t("privacy.deleteAlready")}
          </p>
        ) : (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              void askDeletion();
            }}
          >
            <label htmlFor={`${ids}-note`} className="block text-sm font-medium">
              {t("privacy.deleteNote")}
            </label>
            <Textarea id={`${ids}-note`} value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={1000} />
            <Button type="submit" variant="outline" disabled={deleting}>
              {deleting && <Loader2 className="animate-spin" aria-hidden />}
              {t("privacy.deleteSend")}
            </Button>
          </form>
        )}
        {privacy.data_requests.length > 0 && (
          <ul className="space-y-1 text-sm">
            {privacy.data_requests.map((r) => (
              <li key={r.id} className="flex items-center gap-2">
                <span>{format.dateTime(new Date(r.created_at), { dateStyle: "medium" })}</span>
                <Badge variant={r.status === "open" ? "signal" : "default"}>
                  {t(`privacy.requestStatus.${r.status === "completed" || r.status === "declined" ? r.status : "open"}`)}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

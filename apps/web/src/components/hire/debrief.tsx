// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, Check, HelpCircle, Loader2 } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import { decideDebrief } from "@/lib/actions/interviews";
import type { HireDebrief } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

const DECISIONS = ["advance", "hold", "reject"] as const;
type Decision = (typeof DECISIONS)[number];

/**
 * The debrief on one page (brief 3.7.4, 5.3): criteria and ratings, disagreements, open
 * questions and the interviewers' recommendations, then the hiring manager's decision
 * with a reason. The AI summarises; people decide.
 */
export function DebriefView({ initial, totpEnabled }: { initial: HireDebrief; totpEnabled: boolean }) {
  const t = useTranslations("Debrief");
  const tRec = useTranslations("Scorecard");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const locale = useLocale();
  const ids = useId();
  const [debrief, setDebrief] = useState(initial);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function decide(e: React.FormEvent) {
    e.preventDefault();
    if (!decision) return;
    setPending(true);
    setError(null);
    const result = await decideDebrief({ applicationId: debrief.application_id, stage: debrief.stage_key, decision, reason });
    setPending(false);
    if (result.ok) setDebrief(result.debrief);
    else setError(result.code);
  }

  return (
    <div className="space-y-6" data-debrief data-status={debrief.status}>
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">
          {debrief.job_title} · {debrief.stage_name}
        </p>
        <h1 className="font-display text-2xl">{debrief.candidate_label}</h1>
      </header>

      {debrief.status === "waiting" ? (
        <section className="space-y-2 rounded-xl border bg-surface p-5" data-debrief-waiting>
          <p className="font-medium">{t("waiting", { submitted: debrief.submitted, expected: debrief.expected })}</p>
          {debrief.waiting_for.length > 0 && (
            <p className="text-sm text-muted-foreground">{t("waitingFor", { names: debrief.waiting_for.join(", ") })}</p>
          )}
          <p className="text-sm text-muted-foreground">{t("independent")}</p>
        </section>
      ) : (
        <>
          {debrief.summary && (
            <section className="space-y-2 rounded-xl border bg-surface p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-display text-lg">{t("summary")}</h2>
                <span className="text-xs text-muted-foreground">{debrief.source === "model" ? t("aiNote") : t("numbersNote")}</span>
              </div>
              <p data-debrief-summary>{debrief.summary}</p>
            </section>
          )}

          <section aria-labelledby={`${ids}-criteria`} className="space-y-2">
            <h2 id={`${ids}-criteria`} className="font-display text-lg">
              {t("criteria")}
            </h2>
            <ul className="divide-y rounded-xl border bg-surface">
              {debrief.criteria.map((c) => (
                <li key={c.id} className="space-y-1 p-4 text-sm" data-debrief-criterion={c.id}>
                  <p className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{c.name}</span>
                    {c.mean !== null && c.mean !== undefined && (
                      <span className="text-muted-foreground">{t("mean", { mean: format.number(c.mean) })}</span>
                    )}
                    {c.disagreement && (
                      <Badge variant="signal">
                        <AlertTriangle aria-hidden className="size-3" />
                        {t("disagreement")}
                      </Badge>
                    )}
                  </p>
                  <p className="flex flex-wrap gap-x-3 gap-y-1">
                    {c.ratings.map((r) => (
                      <span key={`${r.label}-${r.interviewer}`}>
                        {r.interviewer}: <span className="font-semibold">{r.rating}</span> / 4
                      </span>
                    ))}
                  </p>
                  {c.summary && <p className="text-muted-foreground">{c.summary}</p>}
                </li>
              ))}
            </ul>
          </section>

          {debrief.disagreements.length > 0 && (
            <section className="space-y-2">
              <h2 className="font-display text-lg">{t("disagreements")}</h2>
              <ul className="list-disc space-y-1 ps-5 text-sm" data-debrief-disagreements>
                {debrief.disagreements.map((d) => (
                  <li key={d}>{d}</li>
                ))}
              </ul>
            </section>
          )}
          {debrief.open_questions.length > 0 && (
            <section className="space-y-2">
              <h2 className="flex items-center gap-2 font-display text-lg">
                <HelpCircle aria-hidden className="size-4" />
                {t("openQuestions")}
              </h2>
              <ul className="list-disc space-y-1 ps-5 text-sm">
                {debrief.open_questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </section>
          )}

          <section className="space-y-2">
            <h2 className="font-display text-lg">{t("recommendations")}</h2>
            <ul className="space-y-2">
              {debrief.voices.map((v) => (
                <li key={v.label} className="rounded-xl border bg-surface p-4 text-sm">
                  <p className="font-medium">
                    {v.interviewer}: {tRec(`recommendation.${v.recommendation}` as "recommendation.yes")}
                  </p>
                  {v.notes && <p className="text-muted-foreground">{v.notes}</p>}
                </li>
              ))}
            </ul>
          </section>

          {debrief.decision ? (
            <p role="status" className="flex items-start gap-2 rounded-xl border bg-surface p-4" data-debrief-decision={debrief.decision.decision}>
              <Check aria-hidden className="mt-0.5 size-5 text-primary" />
              <span>
                <span className="font-medium">{t(`decision.${debrief.decision.decision}` as "decision.advance")}</span>
                {" · "}
                {format.dateTime(new Date(debrief.decision.decided_at), { dateStyle: "medium", timeStyle: "short" })}
                <span className="block text-sm text-muted-foreground">{debrief.decision.reason}</span>
              </span>
            </p>
          ) : debrief.can_decide ? (
            <form onSubmit={decide} className="space-y-3 rounded-xl border bg-surface p-5" data-debrief-form>
              <h2 className="font-display text-lg">{t("decide")}</h2>
              <p className="text-sm text-muted-foreground">{t("decideHint")}</p>
              <div role="radiogroup" aria-label={t("decide")} className="grid grid-cols-3 gap-2">
                {DECISIONS.map((d) => (
                  <button
                    key={d}
                    type="button"
                    role="radio"
                    aria-checked={decision === d}
                    onClick={() => setDecision(d)}
                    data-decision={d}
                    className={cn(
                      "min-h-11 rounded-lg border px-3 text-sm font-medium focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none",
                      decision === d ? "border-primary bg-primary/10" : "bg-surface hover:bg-accent",
                    )}
                  >
                    {t(`decision.${d}`)}
                  </button>
                ))}
              </div>
              <label htmlFor={`${ids}-reason`} className="block text-sm font-medium">
                {t("reason")}
              </label>
              <Textarea
                id={`${ids}-reason`}
                className="min-h-20"
                maxLength={1000}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                data-debrief-reason
              />
              {error === "second_factor_required" || error === "permission_denied" ? (
                <StepUpPrompt
                  totpEnabled={totpEnabled}
                  returnPath={`/${locale}/hire/jobs/${debrief.job_id}/debrief/${debrief.application_id}`}
                  compact
                />
              ) : (
                error && (
                  <p role="alert" className="text-sm text-destructive">
                    {tErrors(errorKey(error) as "generic")}
                  </p>
                )
              )}
              <Button type="submit" disabled={!decision || reason.trim().length < 3 || pending} data-debrief-submit>
                {pending && <Loader2 aria-hidden className="animate-spin" />}
                {t("record")}
              </Button>
            </form>
          ) : null}
        </>
      )}
    </div>
  );
}

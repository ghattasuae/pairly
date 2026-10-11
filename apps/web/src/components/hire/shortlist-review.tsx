// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { CheckCircle2, Loader2 } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import { errorKey } from "@/lib/action-result";
import { addNote, moveCandidate, setTags, submitMessage } from "@/lib/actions/hire";
import { actionKey, nextStage } from "@/lib/hire/board";
import { HOLD_TAG, withTag } from "@/lib/hire/screening";
import {
  currentInterview,
  decisionForKey,
  DECISIONS,
  highlights,
  needsReason,
  REASON_MAX,
  REASON_MIN,
  reasonProblem,
  SHORTLIST_STAGE,
  summarize,
  type Decision,
  type ShortlistCandidate,
} from "@/lib/hire/shortlist";
import type { HireStage } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

export interface ShortlistActions {
  moveCandidate: typeof moveCandidate;
  submitMessage: typeof submitMessage;
  addNote: typeof addNote;
  setTags: typeof setTags;
}

const DEFAULT_ACTIONS: ShortlistActions = { moveCandidate, submitMessage, addNote, setTags };

type Outcome =
  | { kind: "advance"; stage: string }
  | { kind: "hold" }
  | { kind: "reject" }
  | { kind: "rejectMoved"; stage: string }
  | { kind: "draftNotSubmitted"; stage: string; draftId: string };

export interface ShortlistReviewProps {
  jobId: string;
  jobTitle: string;
  stages: HireStage[];
  candidates: ShortlistCandidate[];
  canDecide: boolean;
  canNote: boolean;
  canMessage: boolean;
  totpEnabled: boolean;
  /** Overrides for stories and tests. Default to the server actions. */
  actions?: Partial<ShortlistActions>;
}

function isTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLInputElement) return target.type !== "radio";
  return target.tagName === "TEXTAREA" || target.tagName === "SELECT";
}

/**
 * The hiring manager's shortlist review (brief 5.3): every shortlisted candidate of one
 * job on one screen with the summary, rubric scores, evidence highlights and interview
 * status, and one decision each: advance to the next stage, hold, or draft a rejection
 * into the approval queue. Hold and reject need a reason. Keyboard: J and K move between
 * candidates, A, H and R choose a decision, Ctrl+Enter records it. One column on a phone.
 */
export function ShortlistReview({
  jobId,
  jobTitle,
  stages,
  candidates,
  canDecide,
  canNote,
  canMessage,
  totpEnabled,
  actions: overrides,
}: ShortlistReviewProps) {
  const t = useTranslations("Shortlist");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const actions = useMemo(() => ({ ...DEFAULT_ACTIONS, ...overrides }), [overrides]);
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const [stepUp, setStepUp] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  const headingId = useId();

  const stageLabel = useCallback(
    (key: string) => {
      const stage = stages.find((s) => s.key === key);
      if (!stage) return key;
      return locale === "ar" && stage.label_ar ? stage.label_ar : stage.label;
    },
    [locale, stages],
  );
  const next = nextStage(stages, SHORTLIST_STAGE);
  const done = Object.keys(outcomes).length;

  // J and K work from anywhere on the page (the focus starts on the body); A, H and R
  // choose for the candidate that holds the focus. Never while typing or in a dialog.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      if (isTextField(event.target) || document.querySelector('[role="dialog"]')) return;
      const items = [...(list.current?.querySelectorAll<HTMLElement>("[data-shortlist-candidate]") ?? [])];
      if (items.length === 0) return;
      const key = event.key.toLowerCase();
      const current = items.findIndex((el) => el.contains(document.activeElement));
      if (key === "j" || key === "k") {
        event.preventDefault();
        const delta = key === "j" ? 1 : -1;
        const index = current < 0 ? (delta > 0 ? 0 : items.length - 1) : Math.min(items.length - 1, Math.max(0, current + delta));
        items[index]?.focus();
        items[index]?.scrollIntoView?.({ block: "nearest" });
        return;
      }
      const decision = decisionForKey(key);
      const article = items[current];
      if (!decision || !article) return;
      const radio = article.querySelector<HTMLInputElement>(`input[type="radio"][value="${decision}"]`);
      if (!radio || radio.disabled) return;
      event.preventDefault();
      // Commit the choice now, so the reason field and the record button exist and are enabled.
      flushSync(() => radio.click());
      const target = needsReason(decision)
        ? article.querySelector<HTMLElement>("textarea")
        : article.querySelector<HTMLElement>("[data-record]");
      target?.focus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const record = useCallback(
    async (candidate: ShortlistCandidate, decision: Decision, reason: string): Promise<string | null> => {
      const id = candidate.card.application_id;
      const text = reason.trim();
      setStepUp(false);
      const fail = (code: string) => {
        if (code === "second_factor_required") {
          setStepUp(true);
          return null;
        }
        return tErrors(errorKey(code) as "generic");
      };
      if (decision === "advance") {
        if (!next) return tErrors("generic");
        const result = await actions.moveCandidate({
          applicationId: id,
          toStage: next.key,
          idempotencyKey: actionKey(),
          ...(text ? { reason: text } : {}),
        });
        if (!result.ok) return fail(result.code);
        setOutcomes((o) => ({ ...o, [id]: { kind: "advance", stage: result.move.to_stage } }));
        return null;
      }
      if (decision === "hold") {
        const note = await actions.addNote({ applicationId: id, body: t("holdNote", { reason: text }), mentions: [] });
        if (!note.ok) return fail(note.code);
        const tags = await actions.setTags(id, withTag(candidate.card.tags, HOLD_TAG));
        if (!tags.ok) return fail(tags.code);
        setOutcomes((o) => ({ ...o, [id]: { kind: "hold" } }));
        return null;
      }
      const moved = await actions.moveCandidate({
        applicationId: id,
        toStage: "rejected",
        idempotencyKey: actionKey(),
        reason: text,
      });
      if (!moved.ok) return fail(moved.code);
      const draftId = moved.move.draft_id;
      if (!draftId || !canMessage) {
        setOutcomes((o) => ({ ...o, [id]: { kind: "rejectMoved", stage: moved.move.to_stage } }));
        return null;
      }
      const submitted = await actions.submitMessage(draftId, actionKey());
      if (!submitted.ok) {
        if (submitted.code === "second_factor_required") setStepUp(true);
        setOutcomes((o) => ({ ...o, [id]: { kind: "draftNotSubmitted", stage: moved.move.to_stage, draftId } }));
        return null;
      }
      setOutcomes((o) => ({ ...o, [id]: { kind: "reject" } }));
      return null;
    },
    [actions, canMessage, next, t, tErrors],
  );

  return (
    <div className="mx-auto w-full max-w-4xl space-y-5 px-4 py-6 sm:px-8" data-shortlist-review>
      <header className="space-y-2">
        <p className="eyebrow">{t("eyebrow")}</p>
        <h1 id={headingId} className="font-display text-2xl break-words">
          {t("title")}: {jobTitle}
        </h1>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
          <span data-shortlist-count>{t("count", { count: candidates.length })}</span>
          {candidates.length > 0 && (
            <span role="status" aria-live="polite" data-shortlist-progress>
              {t("progress", { done, total: candidates.length })}
            </span>
          )}
          <Link href={`/hire/jobs/${jobId}/pipeline`} className="text-primary hover:underline">
            {t("backToPipeline")}
          </Link>
        </div>
        {candidates.length > 0 && <p className="hidden text-xs text-muted-foreground sm:block">{t("keysHint")}</p>}
        {!canDecide && candidates.length > 0 && <p className="text-sm text-muted-foreground">{t("noRight")}</p>}
      </header>

      {stepUp && (
        <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/${locale}/hire/jobs/${jobId}/shortlist`} compact />
      )}

      {candidates.length === 0 ? (
        <p className="rounded-xl border bg-surface p-5 text-sm text-muted-foreground">{t("empty")}</p>
      ) : (
        <ol ref={list} className="space-y-4" aria-labelledby={headingId}>
          {candidates.map((candidate) => (
            <li key={candidate.card.application_id}>
              <CandidateReview
                candidate={candidate}
                stageLabel={stageLabel}
                nextLabel={next ? stageLabel(next.key) : null}
                outcome={outcomes[candidate.card.application_id] ?? null}
                canDecide={canDecide}
                canNote={canNote}
                onRecord={record}
                jobId={jobId}
              />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function CandidateReview({
  candidate,
  stageLabel,
  nextLabel,
  outcome,
  canDecide,
  canNote,
  onRecord,
  jobId,
}: {
  candidate: ShortlistCandidate;
  stageLabel: (key: string) => string;
  nextLabel: string | null;
  outcome: Outcome | null;
  canDecide: boolean;
  canNote: boolean;
  onRecord: (candidate: ShortlistCandidate, decision: Decision, reason: string) => Promise<string | null>;
  jobId: string;
}) {
  const t = useTranslations("Shortlist");
  const tSchedule = useTranslations("Scheduling");
  const format = useFormatter();
  const ids = useId();
  const { card, screening } = candidate;
  const [decision, setDecision] = useState<Decision | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const self = useRef<HTMLElement>(null);
  const summary = summarize(screening);
  const quotes = highlights(candidate);
  const interview = currentInterview(candidate.interviews);
  const max = screening?.scale.max ?? 4;
  const held = card.tags.some((tag) => tag.toLowerCase() === HOLD_TAG.toLowerCase());

  const allowed = (d: Decision) => (d === "hold" ? canNote : canDecide && (d !== "advance" || nextLabel !== null));

  const submit = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!decision || pending) return;
    const problem = reasonProblem(decision, reason);
    if (problem) {
      setError(t(`problem.${problem}`, { min: REASON_MIN, max: REASON_MAX }));
      return;
    }
    setError(null);
    setPending(true);
    const failure = await onRecord(candidate, decision, reason);
    setPending(false);
    setError(failure);
    // The form is replaced by the outcome: keep the focus on this candidate, so J goes on.
    if (failure === null) requestAnimationFrame(() => self.current?.focus());
  };

  const outcomeText = (o: Outcome): string => {
    switch (o.kind) {
      case "advance":
        return t("done.advance", { stage: stageLabel(o.stage) });
      case "hold":
        return t("done.hold");
      case "reject":
        return t("done.reject");
      case "rejectMoved":
        return t("done.rejectMoved", { stage: stageLabel(o.stage) });
      case "draftNotSubmitted":
        return t("draftNotSubmitted", { stage: stageLabel(o.stage) });
    }
  };

  return (
    <article
      ref={self}
      tabIndex={-1}
      aria-labelledby={`${ids}-name`}
      className="space-y-4 rounded-xl border bg-surface p-4 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/25 sm:p-5"
      data-shortlist-candidate={card.application_id}
      data-decided={outcome ? outcome.kind : undefined}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 id={`${ids}-name`} className="text-lg font-medium break-words" data-candidate-label>
            {card.label}
          </h2>
          <p className="text-xs text-muted-foreground" dir="ltr">
            {card.reference}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge data-overall-score={screening?.overall_score ?? ""}>
            {screening?.overall_score !== null && screening?.overall_score !== undefined
              ? t("overall", { score: Math.round(screening.overall_score) })
              : t("notScored")}
          </Badge>
          {held && <Badge variant="outline">{HOLD_TAG}</Badge>}
          {card.needs_human_review && <Badge variant="signal">{t("needsPerson")}</Badge>}
        </div>
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <section aria-labelledby={`${ids}-summary`} className="space-y-1.5 text-sm">
          <h3 id={`${ids}-summary`} className="eyebrow">
            {t("summary")}
          </h3>
          {summary.role || summary.skills.length > 0 ? (
            <>
              {summary.role && (
                <p>{summary.employer ? t("roleAt", { role: summary.role, employer: summary.employer }) : summary.role}</p>
              )}
              {summary.skills.length > 0 && (
                <ul className="flex flex-wrap gap-1.5" aria-label={t("skills")}>
                  {summary.skills.map((skill) => (
                    <li key={skill} className="rounded-full border px-2 py-0.5 text-xs" dir="auto">
                      {skill}
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <p className="text-muted-foreground">{t("noSummary")}</p>
          )}
        </section>

        <section aria-labelledby={`${ids}-interview`} className="space-y-1.5 text-sm">
          <h3 id={`${ids}-interview`} className="eyebrow">
            {t("interview")}
          </h3>
          <p data-interview-status={interview?.status ?? "none"}>
            {interview
              ? interview.starts_at
                ? t("interviewAt", {
                    status: tSchedule(`status.${interview.status}` as "status.booked"),
                    when: format.dateTime(new Date(interview.starts_at), { dateStyle: "medium", timeStyle: "short" }),
                  })
                : tSchedule(`status.${interview.status}` as "status.booked")
              : t("noInterview")}
          </p>
        </section>

        <section aria-labelledby={`${ids}-scores`} className="space-y-1.5 text-sm">
          <h3 id={`${ids}-scores`} className="eyebrow">
            {t("scores")}
          </h3>
          {screening && screening.criteria.length > 0 ? (
            <ul className="space-y-1" data-rubric-scores>
              {screening.criteria.map((c) => (
                <li key={c.criterion_id} className="flex items-baseline justify-between gap-3">
                  <span className="min-w-0 break-words">
                    {c.name}
                    {c.must_have && <span className="ms-1.5 text-xs text-muted-foreground">({t("mustHave")})</span>}
                  </span>
                  <span className="shrink-0 tabular-nums" data-criterion-score={c.score}>
                    {t("scoreOf", { score: c.score, max })}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground">{t("noScores")}</p>
          )}
        </section>

        <section aria-labelledby={`${ids}-evidence`} className="space-y-1.5 text-sm">
          <h3 id={`${ids}-evidence`} className="eyebrow">
            {t("evidence")}
          </h3>
          {quotes.length > 0 ? (
            <ul className="space-y-2" data-evidence-highlights>
              {quotes.map((q) => (
                <li key={`${q.criterion}-${q.quote}`}>
                  <p className="text-xs text-muted-foreground">{q.criterion}</p>
                  <blockquote className="border-s-2 border-primary/40 ps-2 break-words" dir="auto">
                    {q.quote}
                  </blockquote>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground">{t("noEvidence")}</p>
          )}
        </section>
      </div>

      {outcome ? (
        <div className="flex flex-wrap items-start gap-2 rounded-lg border bg-accent/30 p-3 text-sm" role="status" data-outcome={outcome.kind}>
          <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
          <p className="min-w-0 flex-1">{outcomeText(outcome)}</p>
          {outcome.kind === "draftNotSubmitted" && (
            <Link
              href={`/hire/jobs/${jobId}/pipeline?candidate=${card.application_id}`}
              className="text-primary hover:underline"
            >
              {t("openDraft")}
            </Link>
          )}
        </div>
      ) : (
        <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
          <fieldset className="space-y-2" disabled={pending}>
            <legend className="text-sm font-medium">{t("decision")}</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              {DECISIONS.map((d) => (
                <label
                  key={d}
                  className={cn(
                    "flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm has-[:checked]:border-primary has-[:checked]:bg-primary/5 has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-50 has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/25",
                  )}
                >
                  <input
                    type="radio"
                    name={`${ids}-decision`}
                    value={d}
                    checked={decision === d}
                    disabled={!allowed(d)}
                    onChange={() => {
                      setDecision(d);
                      setError(null);
                    }}
                    className="accent-primary"
                    data-decision={d}
                  />
                  {d === "advance" ? t("advance", { stage: nextLabel ?? "" }) : t(d)}
                </label>
              ))}
            </div>
          </fieldset>
          {decision && (
            <div className="space-y-1.5">
              <label htmlFor={`${ids}-reason`} className="text-sm font-medium">
                {needsReason(decision) ? t("reasonRequired") : t("reasonOptional")}
              </label>
              <Textarea
                id={`${ids}-reason`}
                rows={2}
                className="min-h-16"
                maxLength={REASON_MAX}
                value={reason}
                required={needsReason(decision)}
                aria-invalid={error ? true : undefined}
                aria-describedby={decision === "advance" ? undefined : `${ids}-reason-help`}
                onChange={(e) => setReason(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    void submit();
                  }
                }}
                disabled={pending}
                data-reason
              />
              {decision !== "advance" && (
                <p id={`${ids}-reason-help`} className="text-xs text-muted-foreground">
                  {decision === "hold" ? t("reasonHelpHold") : t("reasonHelpReject")}
                </p>
              )}
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <Button type="submit" disabled={!decision || pending} className="w-full sm:w-auto" data-record>
            {pending && <Loader2 className="animate-spin" aria-hidden />}
            {pending ? t("recording") : t("record")}
          </Button>
        </form>
      )}
    </article>
  );
}

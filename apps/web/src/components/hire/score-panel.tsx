// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Flag, Info, Loader2, PencilLine, Quote, ShieldQuestion } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import {
  criterionFlagKeys,
  evidenceId,
  flagKey,
  type ScreeningCriterion,
  type ScreeningPanel,
  whyNotHigher,
} from "@/lib/hire/screening";
import { cn } from "@/lib/utils";

/** "Why not higher" in one plain sentence, from the rubric's own anchor text. */
export function WhyNotHigher({ criterion, max }: { criterion: ScreeningCriterion; max: number }) {
  const t = useTranslations("Screen");
  const why = whyNotHigher(criterion, max);
  return (
    <p className="text-sm" data-why={why.key}>
      <span className="font-medium">{t("why.title")}: </span>
      {t(`why.${why.key}`, why.values)}
    </p>
  );
}

function ScoreBar({ score, max, label }: { score: number; max: number; label: string }) {
  return (
    <span
      className="flex items-center gap-0.5"
      role="img"
      aria-label={label}
      data-score={score}
    >
      {Array.from({ length: max }, (_, i) => (
        <span
          key={i}
          className={cn("h-2 w-4 rounded-sm", i < score ? "bg-primary" : "bg-muted")}
          aria-hidden
        />
      ))}
    </span>
  );
}

/**
 * The right side of screening mode: the overall score (computed in code from the rubric),
 * then each criterion with its score, the quotes from the CV that support it (select one
 * to see it in the CV), "why not higher" in one sentence, and a person's override with a
 * required reason. Flags and knockouts follow, each explained in one sentence; flags are
 * information only.
 */
export function ScorePanel({
  screening,
  activeEvidence,
  canOverride,
  onQuote,
  onOverride,
}: {
  screening: ScreeningPanel;
  activeEvidence: string | null;
  canOverride: boolean;
  onQuote: (evidenceId: string) => void;
  onOverride: (criterionId: string, score: number, reason: string) => Promise<boolean>;
}) {
  const t = useTranslations("Screen");
  const max = screening.scale.max ?? 4;

  return (
    <div className="space-y-5" data-score-panel>
      {screening.status === "processing" && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          {t("running")}
        </p>
      )}
      <section aria-labelledby="overall-label" className="rounded-xl border bg-surface p-4">
        <p id="overall-label" className="eyebrow">
          {t("score.overall")}
        </p>
        {screening.overall_score !== null && screening.overall_score !== undefined ? (
          <p className="font-display text-4xl tabular-nums" data-overall>
            {t("score.overallValue", { score: Math.round(screening.overall_score) })}
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">{t("why.notScored")}</p>
        )}
      </section>

      {screening.criteria.length > 0 && (
        <ol className="space-y-3" aria-label={t("scores")}>
          {screening.criteria.map((criterion) => (
            <CriterionItem
              key={criterion.criterion_id}
              criterion={criterion}
              max={max}
              activeEvidence={activeEvidence}
              canOverride={canOverride}
              onQuote={onQuote}
              onOverride={onOverride}
            />
          ))}
        </ol>
      )}

      {screening.flags.length > 0 && (
        <section aria-labelledby="flags-label" className="space-y-2">
          <h3 id="flags-label" className="flex items-center gap-2 text-sm font-medium">
            <Flag className="size-4" aria-hidden />
            {t("score.flags")}
          </h3>
          <p className="text-xs text-muted-foreground">{t("score.flagsInfo")}</p>
          <ul className="space-y-1.5 text-sm">
            {screening.flags.map((flag, index) => (
              <li key={`${flag.code}-${index}`} className="border-s-2 border-signal ps-2" data-flag={flag.code}>
                {t(`flags.${flagKey(flag.code)}` as "flags.unknown")}
              </li>
            ))}
          </ul>
        </section>
      )}

      {screening.knockouts.some((k) => !k.matches) && (
        <section aria-labelledby="knockouts-label" className="space-y-2">
          <h3 id="knockouts-label" className="flex items-center gap-2 text-sm font-medium">
            <ShieldQuestion className="size-4" aria-hidden />
            {t("score.knockouts")}
          </h3>
          <ul className="space-y-1.5 text-sm">
            {screening.knockouts
              .filter((k) => !k.matches)
              .map((k) => (
                <li key={k.id} className="border-s-2 border-signal ps-2">
                  <span className="font-medium">{k.question}</span> {t("score.knockoutMismatch")}
                </li>
              ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function CriterionItem({
  criterion,
  max,
  activeEvidence,
  canOverride,
  onQuote,
  onOverride,
}: {
  criterion: ScreeningCriterion;
  max: number;
  activeEvidence: string | null;
  canOverride: boolean;
  onQuote: (evidenceId: string) => void;
  onOverride: (criterionId: string, score: number, reason: string) => Promise<boolean>;
}) {
  const t = useTranslations("Screen");
  const ids = useId();
  const [editing, setEditing] = useState(false);
  const [score, setScore] = useState(String(criterion.score));
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const flags = criterionFlagKeys(criterion.flags);

  const save = async () => {
    if (reason.trim().length < 10) return;
    setSaving(true);
    const ok = await onOverride(criterion.criterion_id, Number(score), reason.trim());
    setSaving(false);
    if (ok) {
      setEditing(false);
      setReason("");
    }
  };

  return (
    <li className="space-y-2 rounded-xl border bg-surface p-3" data-criterion={criterion.criterion_id}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium" id={`${ids}-name`}>
            {criterion.name}
          </p>
          <p className="text-xs text-muted-foreground">
            {criterion.must_have && <span className="me-2">{t("score.mustHave")}</span>}
            {t("score.weight", { weight: criterion.weight })}
            {criterion.confidence > 0 && (
              <span className="ms-2">{t("score.confidence", { value: Math.round(criterion.confidence * 100) })}</span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span className="text-sm font-medium tabular-nums">{t("score.of", { score: criterion.score, max })}</span>
          <ScoreBar score={criterion.score} max={max} label={t("score.of", { score: criterion.score, max })} />
          {criterion.overridden && <Badge variant="signal">{t("score.overridden")}</Badge>}
        </div>
      </div>

      {criterion.evidence.length > 0 ? (
        <ul className="space-y-1" aria-label={t("score.evidence")}>
          {criterion.evidence.map((e, index) => {
            const id = evidenceId(criterion.criterion_id, index);
            return (
              <li key={id}>
                <button
                  type="button"
                  onClick={() => onQuote(id)}
                  aria-pressed={activeEvidence === id}
                  aria-describedby={`${ids}-name`}
                  title={t("score.showInCv")}
                  className={cn(
                    "flex w-full items-start gap-1.5 rounded-md px-2 py-1 text-start text-sm hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
                    activeEvidence === id && "bg-signal-soft",
                  )}
                  data-quote={id}
                >
                  <Quote className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  <span className="line-clamp-3">{e.quote}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">{t("score.noEvidence")}</p>
      )}

      <WhyNotHigher criterion={criterion} max={max} />
      {flags.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {flags.map((f) => (
            <li key={f} className="flex items-start gap-1">
              <Info className="mt-0.5 size-3 shrink-0" aria-hidden />
              {t(`criterionFlags.${f}` as "criterionFlags.no_evidence")}
            </li>
          ))}
        </ul>
      )}

      {canOverride && !editing && (
        <Button type="button" variant="ghost" size="sm" className="-ms-2" onClick={() => setEditing(true)}>
          <PencilLine aria-hidden />
          {t("override.open")}
        </Button>
      )}
      {editing && (
        <form
          className="space-y-2 rounded-lg border bg-background p-3"
          aria-label={t("override.title", { criterion: criterion.name })}
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <p className="text-sm font-medium">{t("override.title", { criterion: criterion.name })}</p>
          <label className="block space-y-1 text-sm">
            <span>{t("override.score")}</span>
            <Select value={score} onChange={(e) => setScore(e.target.value)} className="h-9 w-24">
              {Array.from({ length: max + 1 }, (_, i) => (
                <option key={i} value={i}>
                  {i}
                </option>
              ))}
            </Select>
          </label>
          <label className="block space-y-1 text-sm">
            <span>{t("override.reason")}</span>
            <Textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              required
              minLength={10}
              maxLength={1000}
              rows={3}
            />
          </label>
          <p className="text-xs text-muted-foreground">{t("override.help")}</p>
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={saving || reason.trim().length < 10}>
              {saving && <Loader2 className="animate-spin" aria-hidden />}
              {t("override.save")}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              {t("override.cancel")}
            </Button>
          </div>
        </form>
      )}
    </li>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { loadCalibrationItem, saveCalibrationLabels, suggestClarification } from "@/lib/actions/calibration";
import type {
  HireCalibrationAgreement,
  HireCalibrationApplication,
  HireCalibrationReport,
  HireCalibrationSample,
} from "@/lib/hire/types";

const SCORES = [0, 1, 2, 3, 4] as const;

/** Agreement per criterion, with the suggestion from the anchors and a model suggestion. */
export function CalibrationReportView({
  report,
  jobId,
  suggest = suggestClarification,
}: {
  report: HireCalibrationReport;
  jobId: string;
  suggest?: typeof suggestClarification;
}) {
  const t = useTranslations("HireCalibration");
  const format = useFormatter();
  const [asked, setAsked] = useState<Record<string, { text?: string; failed?: boolean; busy?: boolean }>>({});
  const pct = (v: number | null) => (v === null ? t("none") : format.number(v, { style: "percent", maximumFractionDigits: 0 }));
  async function ask(c: HireCalibrationAgreement) {
    setAsked((s) => ({ ...s, [c.criterion_id]: { busy: true } }));
    const result = await suggest(jobId, c.criterion_id);
    setAsked((s) => ({
      ...s,
      [c.criterion_id]: result.ok ? { text: result.suggestion.suggestion } : { failed: true },
    }));
  }
  return (
    <section className="space-y-3" data-calibration-report>
      <p className="text-sm text-muted-foreground">
        {t("reportScope", { labels: report.labels, overrides: report.overrides })}
      </p>
      <ul className="divide-y card-hairline">
        {report.criteria.map((c) => {
          const state = asked[c.criterion_id] ?? {};
          return (
            <li key={c.criterion_id} className="space-y-2 p-3 text-sm" data-criterion={c.criterion_id}>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">{c.name}</span>
                {c.low_agreement && <Badge variant="signal">{t("low")}</Badge>}
              </div>
              <p className="text-muted-foreground">
                {t("stats", {
                  n: c.n,
                  exact: pct(c.exact),
                  within: pct(c.within_one),
                })}{" "}
                {c.kappa === null
                  ? t("kappaNote", { note: c.kappa_note ?? "" })
                  : t("kappa", { kappa: format.number(c.kappa, { maximumFractionDigits: 2 }) })}
              </p>
              {c.suggestion && <p data-suggestion>{c.suggestion}</p>}
              {c.low_agreement && (
                <div className="space-y-1">
                  <Button size="sm" variant="outline" disabled={state.busy === true} onClick={() => void ask(c)}>
                    {state.busy && <Loader2 aria-hidden className="animate-spin" />}
                    {t("askModel")}
                  </Button>
                  {state.text && (
                    <p className="rounded-md border border-border bg-surface-sunken p-2" data-model-suggestion>
                      {t("modelSays")} {state.text}
                    </p>
                  )}
                  {state.failed && (
                    <p role="alert" className="text-muted-foreground">
                      {t("modelUnavailable")}
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-muted-foreground">{t("never")}</p>
    </section>
  );
}

function ScoreRow({
  criterion,
  value,
  onChange,
}: {
  criterion: HireCalibrationApplication["criteria"][number];
  value: number | undefined;
  onChange: (score: number) => void;
}) {
  const id = useId();
  return (
    <fieldset className="space-y-1.5 card-hairline p-3" aria-labelledby={id}>
      <legend id={id} className="px-1 text-sm font-medium">
        {criterion.name}
      </legend>
      <p className="text-xs text-muted-foreground">{criterion.evidence}</p>
      <div className="grid gap-1.5 sm:grid-cols-5">
        {SCORES.map((score) => (
          <label key={score} className="flex cursor-pointer gap-2 rounded border p-2 text-xs has-[:checked]:border-primary">
            <input
              type="radio"
              name={`${criterion.id}-${id}`}
              value={score}
              checked={value === score}
              onChange={() => onChange(score)}
              aria-label={`${criterion.name}: ${score}`}
            />
            <span>
              <span className="block font-medium">{score}</span>
              <span className="text-muted-foreground">{criterion.anchors[score]}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/**
 * Calibration mode for people who label (brief 3.3.11): pick an application from the
 * sample, read its CV (blind while blind), and score each criterion 0 to 4 against the
 * anchors. The AI's scores are never shown here, so they cannot anchor the person.
 */
export function CalibrationLabeller({
  sample,
  load = loadCalibrationItem,
  save = saveCalibrationLabels,
}: {
  sample: HireCalibrationSample;
  load?: typeof loadCalibrationItem;
  save?: typeof saveCalibrationLabels;
}) {
  const t = useTranslations("HireCalibration");
  const [item, setItem] = useState<HireCalibrationApplication | null>(null);
  const [scores, setScores] = useState<Record<string, number>>({});
  const [done, setDone] = useState<Set<string>>(new Set(sample.items.filter((i) => i.labelled).map((i) => i.application_id)));
  const [state, setState] = useState<"idle" | "busy" | "saved" | "failed">("idle");
  async function open(applicationId: string) {
    setState("busy");
    const result = await load(applicationId);
    if (!result.ok) {
      setState("failed");
      return;
    }
    setItem(result.item);
    setScores({ ...result.item.my_scores });
    setState("idle");
  }
  async function submit() {
    if (!item) return;
    setState("busy");
    const result = await save(item.application_id, scores);
    if (!result.ok) {
      setState("failed");
      return;
    }
    setDone((d) => new Set(d).add(item.application_id));
    setState("saved");
  }
  if (sample.items.length === 0) return <p className="text-sm text-muted-foreground">{t("nothing")}</p>;
  const complete = item !== null && item.criteria.every((c) => scores[c.id] !== undefined);
  return (
    <section className="space-y-4" data-calibration-labeller>
      <p className="text-sm text-muted-foreground">{t("labelIntro")}</p>
      <ul className="flex flex-wrap gap-2">
        {sample.items.map((i) => (
          <li key={i.application_id}>
            <Button
              size="sm"
              variant={item?.application_id === i.application_id ? "default" : "outline"}
              onClick={() => void open(i.application_id)}
            >
              <span className="font-mono">{i.reference}</span>
              {done.has(i.application_id) && <span className="text-xs">{t("labelled")}</span>}
            </Button>
          </li>
        ))}
      </ul>
      {state === "failed" && (
        <p role="alert" className="text-sm">
          {t("failed")}
        </p>
      )}
      {item && (
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="max-h-[32rem] overflow-y-auto whitespace-pre-wrap card-hairline p-3 text-sm" data-cv-text>
            {item.text ?? t("noText")}
          </div>
          <div className="space-y-3">
            {item.criteria.map((c) => (
              <ScoreRow key={c.id} criterion={c} value={scores[c.id]} onChange={(s) => setScores((cur) => ({ ...cur, [c.id]: s }))} />
            ))}
            <div className="flex items-center gap-3">
              <Button disabled={!complete || state === "busy"} onClick={() => void submit()}>
                {state === "busy" && <Loader2 aria-hidden className="animate-spin" />}
                {t("save")}
              </Button>
              {state === "saved" && <span role="status">{t("saved")}</span>}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

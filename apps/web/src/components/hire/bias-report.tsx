// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { judgementTone } from "@/lib/hire/bias";
import type { HireBiasCategory, HireBiasGroup, HireBiasReport, HireBiasStep } from "@/lib/hire/types";

function GroupRow({ group }: { group: HireBiasGroup }) {
  const t = useTranslations("HireBias");
  const format = useFormatter();
  const locale = useLocale();
  const label = locale === "ar" ? group.label_ar : group.label_en;
  const pct = (v: number | null) =>
    v === null ? "" : format.number(v, { style: "percent", maximumFractionDigits: 1 });
  return (
    <tr className="border-t" data-group={group.group}>
      <th scope="row" className="py-2 pe-3 text-start font-medium">
        {label}
      </th>
      {group.suppressed ? (
        <td colSpan={4} className="py-2 text-muted-foreground">
          {t("suppressed")}
        </td>
      ) : (
        <>
          <td className="py-2 pe-3 tabular-nums">{t("counts", { selected: group.selected ?? 0, base: group.base ?? 0 })}</td>
          <td className="py-2 pe-3 tabular-nums">{pct(group.rate)}</td>
          <td className="py-2 pe-3 tabular-nums">
            {group.impact_ratio === null ? "" : format.number(group.impact_ratio, { maximumFractionDigits: 2 })}
          </td>
          <td className="py-2 pe-3 tabular-nums">
            {group.fisher_p === null ? "" : format.number(group.fisher_p, { maximumFractionDigits: 3 })}
          </td>
        </>
      )}
      <td className="py-2">
        <Badge variant={judgementTone(group.judgement)}>{t(`judgement.${group.judgement}` as "judgement.reference")}</Badge>
      </td>
    </tr>
  );
}

function Step({ step, stageLabel }: { step: HireBiasStep; stageLabel: (key: string) => string }) {
  const t = useTranslations("HireBias");
  return (
    <div className="overflow-x-auto rounded-lg border p-3" data-step={`${step.from_stage}:${step.to_stage}`}>
      <h4 className="pb-2 text-sm font-medium">
        {t("step", { from: stageLabel(step.from_stage), to: stageLabel(step.to_stage) })}
      </h4>
      <table className="w-full text-sm">
        <thead className="text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.group")}
            </th>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.counts")}
            </th>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.rate")}
            </th>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.ratio")}
            </th>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.p")}
            </th>
            <th scope="col" className="pb-1 text-start font-normal">
              {t("col.result")}
            </th>
          </tr>
        </thead>
        <tbody>
          {step.groups.map((g) => (
            <GroupRow key={g.group} group={g} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Category({ category, stageLabel }: { category: HireBiasCategory; stageLabel: (key: string) => string }) {
  const t = useTranslations("HireBias");
  const locale = useLocale();
  const id = useId();
  return (
    <section aria-labelledby={id} className="space-y-3" data-category={category.category}>
      <h3 id={id} className="font-display text-lg">
        {locale === "ar" ? category.label_ar : category.label_en}
      </h3>
      {category.steps.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("noAnswers")}</p>
      ) : (
        category.steps.map((s) => <Step key={`${s.from_stage}:${s.to_stage}`} step={s} stageLabel={stageLabel} />)
      )}
    </section>
  );
}

interface FairnessRunSummary {
  pairs?: number;
  flagged?: number;
  redaction_complete?: number;
  input_identical?: number;
  model_deltas?: string;
  threshold?: number;
}

/** The latest counterfactual fairness run: measured redaction, honest model status. */
function FairnessSummary({ summary, action }: { summary: Record<string, unknown> | null; action: React.ReactNode }) {
  const t = useTranslations("HireBias.fairness");
  const run = summary as FairnessRunSummary | null;
  return (
    <div className="space-y-2 rounded-lg border p-4 text-sm" data-fairness>
      <p className="font-medium">{t("title")}</p>
      {run ? (
        <>
          <p className="text-muted-foreground">
            {t("summary", {
              pairs: run.pairs ?? 0,
              complete: run.redaction_complete ?? 0,
              identical: run.input_identical ?? 0,
            })}
          </p>
          <p className="text-muted-foreground" data-model-deltas={run.model_deltas}>
            {run.model_deltas === "measured"
              ? t("measured", { flagged: run.flagged ?? 0, threshold: run.threshold ?? 0 })
              : t("pending")}
          </p>
        </>
      ) : (
        <p className="text-muted-foreground">{t("none")}</p>
      )}
      {action}
    </div>
  );
}

/**
 * The bias and adverse-impact report (brief 3.10): selection rates per voluntary
 * self-identified group at each pipeline step, impact ratios against the four-fifths rule
 * of thumb, Fisher exact tests for small samples, small groups hidden. Counts only: it
 * never names or links a candidate. The export link downloads the audit pack (the API
 * asks for a two-factor sign-in and audits it).
 */
export function BiasReport({
  report,
  exportHref,
  stageLabels = {},
  fairnessAction = null,
}: {
  report: HireBiasReport;
  exportHref: string | null;
  stageLabels?: Record<string, string>;
  fairnessAction?: React.ReactNode;
}) {
  const t = useTranslations("HireBias");
  const stageLabel = (key: string) => stageLabels[key] ?? key;
  return (
    <div className="space-y-8" data-bias-report>
      <div className="space-y-2 rounded-lg border p-4 text-sm">
        <p>{t("scope", { count: report.applications })}</p>
        <p className="text-muted-foreground">{t("method", { min: report.settings.min_sample, group: report.settings.min_group })}</p>
        <p className="text-muted-foreground">{t("notLegal")}</p>
        {exportHref ? (
          <p>
            <a href={exportHref} download className="text-primary hover:underline" data-bias-export>
              {t("export")}
            </a>{" "}
            <span className="text-xs text-muted-foreground">{t("exportNote")}</span>
          </p>
        ) : null}
      </div>
      <FairnessSummary summary={report.fairness ?? null} action={fairnessAction} />
      {report.categories.map((c) => (
        <Category key={c.category} category={c} stageLabel={stageLabel} />
      ))}
    </div>
  );
}

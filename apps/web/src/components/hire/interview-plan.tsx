// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2, Plus, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorKey } from "@/lib/action-result";
import { saveInterviewPlan } from "@/lib/actions/interviews";
import type { HireInterviewPlan, HireInterviewPlanView, HirePlanQuestion, HirePlanStage } from "@/lib/hire/types";

const MIN_Q = 3;
const MAX_Q = 5;
const MAX_STAGES = 6;

function nextCriterion(view: HireInterviewPlanView, stage: HirePlanStage): string | null {
  const used = new Set(stage.questions.map((q) => q.criterion_id));
  return view.criteria.find((c) => !used.has(c.id))?.id ?? null;
}

/**
 * The job's interview plan (brief 3.1.5): stages, interviewers per stage, and a scorecard
 * per stage of 3 to 5 questions mapped to rubric criteria, each rated 1 to 4 with anchor
 * descriptions. Saved as one version; a stale save asks to reload.
 */
export function InterviewPlanEditor({ jobId, initial }: { jobId: string; initial: HireInterviewPlanView }) {
  const t = useTranslations("InterviewPlan");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const ids = useId();
  const [view, setView] = useState(initial);
  const [plan, setPlan] = useState<HireInterviewPlan | null>(initial.plan ?? null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  if (!plan) {
    return (
      <section className="space-y-2 rounded-xl border bg-surface p-5" data-interview-plan>
        <h2 className="font-display text-xl">{t("title")}</h2>
        <p className="text-sm text-muted-foreground">{view.rubric_locked ? t("needCriteria") : t("lockFirst")}</p>
      </section>
    );
  }
  const editable = view.can_edit;
  const criterionName = (id: string) => view.criteria.find((c) => c.id === id)?.name ?? id;

  function setStage(index: number, change: Partial<HirePlanStage>) {
    setPlan((p) => (p ? { ...p, stages: p.stages.map((s, i) => (i === index ? { ...s, ...change } : s)) } : p));
  }
  function setQuestion(si: number, qi: number, change: Partial<HirePlanQuestion>) {
    setPlan((p) =>
      p
        ? {
            ...p,
            stages: p.stages.map((s, i) =>
              i === si ? { ...s, questions: s.questions.map((q, j) => (j === qi ? { ...q, ...change } : q)) } : s,
            ),
          }
        : p,
    );
  }
  function addStage() {
    if (!plan) return;
    const free = view.pipeline_stages.find((s) => !plan.stages.some((x) => x.key === s.key));
    if (!free) return;
    const first = plan.stages[0];
    setPlan({
      ...plan,
      stages: [
        ...plan.stages,
        { key: free.key, name: free.label, name_ar: free.label_ar, interviewer_ids: [], questions: first ? first.questions : [] },
      ],
    });
  }

  async function save() {
    if (!plan) return;
    setBusy(true);
    setMessage(null);
    const result = await saveInterviewPlan(jobId, plan, view.version ?? null);
    setBusy(false);
    if (!result.ok) {
      setMessage({ ok: false, text: tErrors(errorKey(result.code) as "generic") });
      return;
    }
    setView(result.view);
    setPlan(result.view.plan ?? plan);
    setMessage({ ok: true, text: t("saved", { version: result.view.version ?? 1 }) });
  }

  const anchors = locale === "ar" ? view.default_anchors_ar : view.default_anchors;
  return (
    <section className="space-y-5 rounded-xl border bg-surface p-5" data-interview-plan>
      <div className="space-y-1">
        <h2 className="font-display text-xl">{t("title")}</h2>
        <p className="text-sm text-muted-foreground">{t("intro")}</p>
        {!view.saved && <p className="text-xs text-muted-foreground">{t("defaultNote")}</p>}
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-primary">{t("anchorsTitle")}</summary>
        <ol className="mt-2 list-inside list-decimal space-y-1 text-muted-foreground">
          {anchors.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ol>
      </details>
      {plan.stages.map((stage, si) => (
        <fieldset key={`${stage.key}-${si}`} className="space-y-4 rounded-lg border p-4" data-plan-stage={stage.key}>
          <legend className="px-1 text-sm font-medium">{t("stage", { n: si + 1 })}</legend>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-${si}-key`}>{t("pipelineStage")}</Label>
              <Select
                id={`${ids}-${si}-key`}
                disabled={!editable}
                value={stage.key}
                onChange={(e) => setStage(si, { key: e.target.value })}
              >
                {view.pipeline_stages.map((p) => (
                  <option key={p.key} value={p.key}>
                    {locale === "ar" && p.label_ar ? p.label_ar : p.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-${si}-name`}>{t("name")}</Label>
              <Input
                id={`${ids}-${si}-name`}
                disabled={!editable}
                maxLength={80}
                value={stage.name}
                onChange={(e) => setStage(si, { name: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-${si}-name-ar`}>{t("nameAr")}</Label>
              <Input
                id={`${ids}-${si}-name-ar`}
                dir="rtl"
                lang="ar"
                disabled={!editable}
                maxLength={80}
                value={stage.name_ar ?? ""}
                onChange={(e) => setStage(si, { name_ar: e.target.value })}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <p className="text-sm font-medium">{t("interviewers")}</p>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {view.team.map((person) => {
                const on = (stage.interviewer_ids ?? []).includes(person.id);
                return (
                  <label key={person.id} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      disabled={!editable}
                      checked={on}
                      onChange={() =>
                        setStage(si, {
                          interviewer_ids: on
                            ? (stage.interviewer_ids ?? []).filter((x) => x !== person.id)
                            : [...(stage.interviewer_ids ?? []), person.id],
                        })
                      }
                    />
                    {person.name}
                  </label>
                );
              })}
            </div>
          </div>
          <ol className="space-y-3">
            {stage.questions.map((q, qi) => (
              <li key={`${q.criterion_id}-${qi}`} className="space-y-2 rounded-md bg-muted/40 p-3" data-plan-question={q.criterion_id}>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="min-w-48 flex-1 space-y-1.5">
                    <Label htmlFor={`${ids}-${si}-${qi}-c`}>{t("criterion")}</Label>
                    <Select
                      id={`${ids}-${si}-${qi}-c`}
                      disabled={!editable}
                      value={q.criterion_id}
                      onChange={(e) => setQuestion(si, qi, { criterion_id: e.target.value })}
                    >
                      {view.criteria.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name} ({t(`kind.${c.kind}` as "kind.must")})
                        </option>
                      ))}
                    </Select>
                  </div>
                  {editable && stage.questions.length > MIN_Q && (
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      aria-label={t("removeQuestion", { name: criterionName(q.criterion_id) })}
                      onClick={() => setStage(si, { questions: stage.questions.filter((_, j) => j !== qi) })}
                    >
                      <Trash2 aria-hidden />
                    </Button>
                  )}
                </div>
                <Label htmlFor={`${ids}-${si}-${qi}-q`}>{t("question")}</Label>
                <Textarea
                  id={`${ids}-${si}-${qi}-q`}
                  className="min-h-16"
                  disabled={!editable}
                  maxLength={300}
                  value={q.question}
                  onChange={(e) => setQuestion(si, qi, { question: e.target.value })}
                />
                <Label htmlFor={`${ids}-${si}-${qi}-qa`}>{t("questionAr")}</Label>
                <Textarea
                  id={`${ids}-${si}-${qi}-qa`}
                  className="min-h-16"
                  dir="rtl"
                  lang="ar"
                  disabled={!editable}
                  maxLength={300}
                  value={q.question_ar ?? ""}
                  onChange={(e) => setQuestion(si, qi, { question_ar: e.target.value })}
                />
                <details>
                  <summary className="cursor-pointer text-xs text-primary">{t("customAnchors")}</summary>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    {[0, 1, 2, 3].map((n) => (
                      <Input
                        key={n}
                        aria-label={t("anchorFor", { rating: n + 1 })}
                        disabled={!editable}
                        placeholder={anchors[n]}
                        maxLength={200}
                        value={(q.anchors ?? [])[n] ?? ""}
                        onChange={(e) => {
                          const next = [0, 1, 2, 3].map((m) => (m === n ? e.target.value : (q.anchors ?? [])[m] ?? ""));
                          setQuestion(si, qi, { anchors: next.every((a) => !a.trim()) ? [] : next });
                        }}
                      />
                    ))}
                  </div>
                </details>
              </li>
            ))}
          </ol>
          {editable && (
            <div className="flex flex-wrap gap-2">
              {stage.questions.length < MAX_Q && nextCriterion(view, stage) && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    const id = nextCriterion(view, stage);
                    if (!id) return;
                    setStage(si, {
                      questions: [...stage.questions, { criterion_id: id, question: "", question_ar: "", anchors: [] }],
                    });
                  }}
                >
                  <Plus aria-hidden />
                  {t("addQuestion")}
                </Button>
              )}
              {plan.stages.length > 1 && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setPlan({ ...plan, stages: plan.stages.filter((_, i) => i !== si) })}
                >
                  <Trash2 aria-hidden />
                  {t("removeStage")}
                </Button>
              )}
            </div>
          )}
        </fieldset>
      ))}
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor={`${ids}-sla`}>{t("sla")}</Label>
          <Input
            id={`${ids}-sla`}
            className="w-28"
            type="number"
            min={4}
            max={168}
            disabled={!editable}
            value={plan.sla_hours ?? 24}
            onChange={(e) => setPlan({ ...plan, sla_hours: Number(e.target.value) })}
          />
        </div>
        {editable && plan.stages.length < MAX_STAGES && (
          <Button type="button" variant="outline" onClick={addStage}>
            <Plus aria-hidden />
            {t("addStage")}
          </Button>
        )}
        {editable && (
          <Button type="button" disabled={busy} onClick={() => void save()} data-save-plan>
            {busy && <Loader2 aria-hidden className="animate-spin" />}
            {t("save")}
          </Button>
        )}
      </div>
      {message && (
        <p role={message.ok ? "status" : "alert"} className={message.ok ? "text-sm text-muted-foreground" : "text-sm text-destructive"}>
          {message.text}
        </p>
      )}
    </section>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Lock, Plus, Send, Sparkles, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox, FieldRow, Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useRouter } from "@/i18n/navigation";
import { errorKey } from "@/lib/action-result";
import {
  checkJobText,
  createJob,
  draftJobDescription,
  draftJobRubric,
  lockJobRubric,
  publishJob,
  saveJobRubric,
  updateJob,
} from "@/lib/actions/jobs";
import {
  applySuggestion,
  EMPLOYMENT_TYPES,
  EMPTY_FIELDS,
  fieldsOf,
  fieldsProblem,
  jobBody,
  newCriterion,
  newKnockout,
  rubricBody,
  rubricFrom,
  rubricProblem,
  SALARY_PERIODS,
  SCALE_LEVELS,
  WORK_MODELS,
  type JobFields,
  type RubricCriterion,
  type RubricDraft,
  type RubricKnockout,
  type RubricProblem,
} from "@/lib/hire/job-editor";
import type { HireFinding, HireJob, HireRubricState, HireTextChecks } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

export interface JobEditorActions {
  draftDescription: typeof draftJobDescription;
  checkText: typeof checkJobText;
  create: typeof createJob;
  update: typeof updateJob;
  draftRubric: typeof draftJobRubric;
  saveRubric: typeof saveJobRubric;
  lockRubric: typeof lockJobRubric;
  publish: typeof publishJob;
}

const DEFAULT_ACTIONS: JobEditorActions = {
  draftDescription: draftJobDescription,
  checkText: checkJobText,
  create: createJob,
  update: updateJob,
  draftRubric: draftJobRubric,
  saveRubric: saveJobRubric,
  lockRubric: lockJobRubric,
  publish: publishJob,
};

type Notice = { tone: "ok" | "error" | "info"; text: string } | null;
type Busy = null | "draft" | "check" | "save" | "rubricDraft" | "rubricSave" | "lock" | "publish";

/**
 * Create or edit a job (brief 5.3): a short brief, the AI-drafted description in English
 * (Arabic optional) with the inclusive-language suggestions inline (one click accepts
 * one) and the requirement warnings, the salary range with show or hide, then the
 * AI-drafted rubric, editable, locked with a second factor, and publishing. When no model
 * answer is available the page says so and the person writes the text themselves.
 * Nothing here decides access: the API checks every call.
 */
export function JobEditor({
  job: initialJob = null,
  rubric: initialRubric = null,
  checks: initialChecks = null,
  canRubric = true,
  canLock = true,
  canPublish = true,
  totpEnabled,
  actions = DEFAULT_ACTIONS,
}: {
  job?: HireJob | null;
  rubric?: HireRubricState | null;
  checks?: HireTextChecks | null;
  canRubric?: boolean;
  canLock?: boolean;
  canPublish?: boolean;
  totpEnabled: boolean;
  actions?: JobEditorActions;
}) {
  const t = useTranslations("HireJob");
  const tErr = useTranslations("Errors");
  const locale = useLocale();
  const router = useRouter();
  const ids = useId();

  const [job, setJob] = useState<HireJob | null>(initialJob);
  const [brief, setBrief] = useState("");
  const [fields, setFields] = useState<JobFields>(initialJob ? fieldsOf(initialJob) : EMPTY_FIELDS);
  const [savedFields, setSavedFields] = useState<JobFields | null>(initialJob ? fieldsOf(initialJob) : null);
  const [arabic, setArabic] = useState(Boolean(initialJob?.description_ar || initialJob?.title_ar));
  const [checks, setChecks] = useState<HireTextChecks | null>(initialChecks);
  const [rubricState, setRubricState] = useState<HireRubricState | null>(initialRubric);
  const [rubric, setRubric] = useState<RubricDraft | null>(
    rubricFrom(initialRubric?.draft ?? initialRubric?.locked, initialJob?.title ?? ""),
  );
  const [savedRubric, setSavedRubric] = useState<string | null>(
    initialRubric?.draft ? JSON.stringify(rubricBody(rubricFrom(initialRubric.draft, initialJob?.title ?? "")!)) : null,
  );
  const [busy, setBusy] = useState<Busy>(null);
  const [jobNotice, setJobNotice] = useState<Notice>(null);
  const [rubricNotice, setRubricNotice] = useState<Notice>(null);
  const [stepUp, setStepUp] = useState(false);
  const [problem, setProblem] = useState<RubricProblem | null>(null);

  const jobDirty = !savedFields || JSON.stringify(jobBody(fields)) !== JSON.stringify(jobBody(savedFields));
  const rubricJson = rubric ? JSON.stringify(rubricBody(rubric)) : null;
  const rubricDirty = rubric !== null && rubricJson !== savedRubric;
  const locked = rubricState?.locked_version ?? null;
  const needsLock = rubric !== null && (rubricDirty || !locked || rubricState?.draft_differs === true);
  const returnPath = job ? `/${locale}/hire/jobs/${job.id}` : `/${locale}/hire/jobs/new`;
  const fieldError = fieldsProblem(fields);

  const fail = (code: string, set: (n: Notice) => void, unavailable?: string) => {
    if (code === "second_factor_required") setStepUp(true);
    if (code === "model_unavailable" && unavailable) {
      set({ tone: "info", text: unavailable });
      return;
    }
    set({ tone: "error", text: tErr(errorKey(code) as "generic") });
  };

  const set = <K extends keyof JobFields>(key: K, value: JobFields[K]) => setFields((prev) => ({ ...prev, [key]: value }));

  // ---------------------------------------------------------------- description

  const draft = async () => {
    setBusy("draft");
    setJobNotice(null);
    const result = await actions.draftDescription(brief);
    setBusy(null);
    if (!result.ok) {
      fail(result.code, setJobNotice, t("draft.unavailable"));
      return;
    }
    const d = result.draft;
    setFields((prev) => ({
      ...prev,
      title: d.title || prev.title,
      title_ar: d.title_ar || prev.title_ar,
      description_en: d.description_en,
      description_ar: d.description_ar,
    }));
    if (d.description_ar || d.title_ar) setArabic(true);
    setChecks(d.checks);
    setJobNotice({ tone: "ok", text: t("draft.done") });
  };

  const recheck = async (next: JobFields = fields) => {
    setBusy("check");
    const result = await actions.checkText({
      title: next.title.slice(0, 160),
      title_ar: arabic ? next.title_ar.slice(0, 160) : "",
      description_en: next.description_en,
      description_ar: arabic ? next.description_ar : "",
    });
    setBusy(null);
    if (result.ok) setChecks(result.checks);
    else fail(result.code, setJobNotice);
  };

  const accept = async (lang: "en" | "ar", finding: HireFinding, suggestion: string) => {
    const titleKey = lang === "en" ? "title" : "title_ar";
    const descKey = lang === "en" ? "description_en" : "description_ar";
    const applied = applySuggestion(fields[titleKey], fields[descKey], finding, suggestion);
    if (!applied) {
      setJobNotice({ tone: "info", text: t("checks.stale") });
      return;
    }
    const next = { ...fields, [titleKey]: applied.title, [descKey]: applied.description };
    setFields(next);
    setJobNotice({ tone: "ok", text: t("checks.accepted", { word: suggestion }) });
    await recheck(next);
  };

  const save = async () => {
    if (fieldError) {
      setJobNotice({ tone: "error", text: t(`problem.${fieldError}`) });
      return;
    }
    const body = jobBody(arabic ? fields : { ...fields, title_ar: "", description_ar: "" });
    setBusy("save");
    setJobNotice(null);
    const result = job ? await actions.update(job.id, body) : await actions.create(body);
    setBusy(null);
    if (!result.ok) {
      fail(result.code, setJobNotice);
      return;
    }
    setJob(result.job);
    const next = fieldsOf(result.job);
    setFields(next);
    setSavedFields(next);
    setJobNotice({ tone: "ok", text: t("saved") });
    if (!job) router.replace(`/hire/jobs/${result.job.id}`);
    else void recheck(next);
  };

  // ---------------------------------------------------------------- rubric

  const adopt = (state: HireRubricState) => {
    setRubricState(state);
    const next = rubricFrom(state.draft ?? state.locked, job?.title ?? "");
    setRubric(next);
    setSavedRubric(next && state.draft ? JSON.stringify(rubricBody(next)) : null);
  };

  const draftRubric = async () => {
    if (!job) return;
    setBusy("rubricDraft");
    setRubricNotice(null);
    const result = await actions.draftRubric(job.id);
    setBusy(null);
    if (!result.ok) {
      fail(result.code, setRubricNotice, t("rubric.unavailable"));
      if (result.code === "model_unavailable" && !rubric) {
        setRubric({ schema_version: 1, job_title: job.title, must_haves: [newCriterion([])], nice_to_haves: [], knockout_questions: [] });
      }
      return;
    }
    adopt(result.rubric);
    setRubricNotice({ tone: "ok", text: t("rubric.drafted") });
  };

  const writeYourself = () => {
    if (!job) return;
    setRubric({ schema_version: 1, job_title: job.title, must_haves: [newCriterion([])], nice_to_haves: [], knockout_questions: [] });
  };

  const persistRubric = async (): Promise<boolean> => {
    if (!job || !rubric) return false;
    const found = rubricProblem(rubric);
    setProblem(found);
    if (found) {
      setRubricNotice({ tone: "error", text: t("rubric.problem") });
      return false;
    }
    setBusy("rubricSave");
    const result = await actions.saveRubric(job.id, rubricBody(rubric));
    setBusy(null);
    if (!result.ok) {
      fail(result.code, setRubricNotice);
      return false;
    }
    adopt(result.rubric);
    return true;
  };

  const saveRubric = async () => {
    setRubricNotice(null);
    if (await persistRubric()) setRubricNotice({ tone: "ok", text: t("rubric.saved") });
  };

  const lock = async () => {
    if (!job) return;
    setRubricNotice(null);
    setStepUp(false);
    if (rubricDirty && !(await persistRubric())) return;
    setBusy("lock");
    const result = await actions.lockRubric(job.id);
    setBusy(null);
    if (!result.ok) {
      fail(result.code, setRubricNotice);
      return;
    }
    const version = result.lock.version;
    setRubricState((prev) =>
      prev ? { ...prev, locked_version: version, locked: prev.draft, draft_differs: false } : prev,
    );
    setJob((prev) => (prev ? { ...prev, rubric_policy_id: version.id } : prev));
    setRubricNotice({ tone: "ok", text: t("rubric.locked", { version: version.version }) });
  };

  const publish = async () => {
    if (!job) return;
    setRubricNotice(null);
    setStepUp(false);
    setBusy("publish");
    const result = await actions.publish(job.id);
    setBusy(null);
    if (!result.ok) {
      if (result.code === "conflict") setRubricNotice({ tone: "error", text: t("publish.conflict") });
      else fail(result.code, setRubricNotice);
      return;
    }
    setJob(result.job);
    setRubricNotice({ tone: "ok", text: t("publish.done") });
  };

  const canPublishNow =
    canPublish && job !== null && job.rubric_policy_id !== null && !needsLock && !jobDirty && (job.status === "draft" || job.status === "paused");

  return (
    <div className="space-y-8" data-job-editor>
      {/* ---------------------------------------------------------- brief */}
      <section aria-labelledby={`${ids}-brief`} className="space-y-3 rounded-xl border bg-surface p-5">
        <h2 id={`${ids}-brief`} className="eyebrow">
          {t("brief.title")}
        </h2>
        <FieldRow label={t("brief.label")} htmlFor={`${ids}-brief-text`} hint={t("brief.hint")}>
          <Textarea
            id={`${ids}-brief-text`}
            rows={3}
            maxLength={800}
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            placeholder={t("brief.placeholder")}
            data-brief
          />
        </FieldRow>
        <Button type="button" onClick={draft} disabled={busy !== null || brief.trim().length < 10} data-draft-jd>
          <Sparkles aria-hidden />
          {busy === "draft" ? t("draft.working") : t("draft.button")}
        </Button>
      </section>

      {/* ---------------------------------------------------------- job */}
      <section aria-labelledby={`${ids}-job`} className="space-y-5 rounded-xl border bg-surface p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id={`${ids}-job`} className="eyebrow">
            {t("job.title")}
          </h2>
          {job && (
            <Badge variant={job.status === "open" ? "default" : "outline"} data-job-status={job.status}>
              {t(`status.${job.status}` as "status.draft")}
            </Badge>
          )}
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <FieldRow label={t("job.jobTitle")} htmlFor={`${ids}-title`}>
            <Input id={`${ids}-title`} value={fields.title} maxLength={160} onChange={(e) => set("title", e.target.value)} data-field="title" />
          </FieldRow>
          <FieldRow label={t("job.team")} htmlFor={`${ids}-team`}>
            <Input id={`${ids}-team`} value={fields.team} maxLength={160} onChange={(e) => set("team", e.target.value)} />
          </FieldRow>
          <FieldRow label={t("job.location")} htmlFor={`${ids}-location`}>
            <Input
              id={`${ids}-location`}
              value={fields.location}
              maxLength={160}
              onChange={(e) => set("location", e.target.value)}
              data-field="location"
            />
          </FieldRow>
          <FieldRow label={t("job.country")} htmlFor={`${ids}-country`} hint={t("job.countryHint")}>
            <Input
              id={`${ids}-country`}
              value={fields.country}
              maxLength={2}
              dir="ltr"
              onChange={(e) => set("country", e.target.value.toUpperCase())}
            />
          </FieldRow>
          <FieldRow label={t("job.workModel")} htmlFor={`${ids}-work`}>
            <Select
              id={`${ids}-work`}
              value={fields.work_model}
              onChange={(e) => set("work_model", e.target.value as JobFields["work_model"])}
              data-field="work_model"
            >
              {WORK_MODELS.map((w) => (
                <option key={w} value={w}>
                  {t(`workModel.${w}`)}
                </option>
              ))}
            </Select>
          </FieldRow>
          <FieldRow label={t("job.employment")} htmlFor={`${ids}-employment`}>
            <Select
              id={`${ids}-employment`}
              value={fields.employment_type}
              onChange={(e) => set("employment_type", e.target.value as JobFields["employment_type"])}
            >
              {EMPLOYMENT_TYPES.map((w) => (
                <option key={w} value={w}>
                  {t(`employment.${w}`)}
                </option>
              ))}
            </Select>
          </FieldRow>
        </div>

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium">{t("salary.title")}</legend>
          <div className="grid gap-4 sm:grid-cols-4">
            <FieldRow label={t("salary.min")} htmlFor={`${ids}-smin`}>
              <Input
                id={`${ids}-smin`}
                inputMode="decimal"
                dir="ltr"
                value={fields.salary_min}
                onChange={(e) => set("salary_min", e.target.value.trim())}
                aria-invalid={fieldError === "salary" || undefined}
              />
            </FieldRow>
            <FieldRow label={t("salary.max")} htmlFor={`${ids}-smax`}>
              <Input
                id={`${ids}-smax`}
                inputMode="decimal"
                dir="ltr"
                value={fields.salary_max}
                onChange={(e) => set("salary_max", e.target.value.trim())}
                aria-invalid={fieldError === "salary" || undefined}
              />
            </FieldRow>
            <FieldRow label={t("salary.currency")} htmlFor={`${ids}-scur`}>
              <Input
                id={`${ids}-scur`}
                maxLength={3}
                dir="ltr"
                value={fields.salary_currency}
                onChange={(e) => set("salary_currency", e.target.value.toUpperCase())}
              />
            </FieldRow>
            <FieldRow label={t("salary.period")} htmlFor={`${ids}-sper`}>
              <Select
                id={`${ids}-sper`}
                value={fields.salary_period}
                onChange={(e) => set("salary_period", e.target.value as JobFields["salary_period"])}
              >
                {SALARY_PERIODS.map((p) => (
                  <option key={p} value={p}>
                    {t(`salary.per.${p}`)}
                  </option>
                ))}
              </Select>
            </FieldRow>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={fields.show_salary} onChange={(e) => set("show_salary", e.target.checked)} data-show-salary />
            {t("salary.show")}
          </label>
          <p className="text-xs text-muted-foreground">{fields.show_salary ? t("salary.shown") : t("salary.hidden")}</p>
        </fieldset>

        <TextBlock
          id={`${ids}-den`}
          label={t("job.descriptionEn")}
          value={fields.description_en}
          onChange={(v) => set("description_en", v)}
          dir="ltr"
          lang="en"
          field="description_en"
        />
        <Findings
          lang="en"
          inclusive={checks?.inclusive_en ?? []}
          sanity={checks?.sanity_en ?? []}
          disabled={busy !== null}
          onAccept={accept}
        />

        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={arabic} onChange={(e) => setArabic(e.target.checked)} data-arabic-toggle />
          {t("job.arabicToggle")}
        </label>
        {arabic && (
          <div className="space-y-4">
            <FieldRow label={t("job.titleAr")} htmlFor={`${ids}-tar`}>
              <Input id={`${ids}-tar`} dir="rtl" lang="ar" value={fields.title_ar} maxLength={160} onChange={(e) => set("title_ar", e.target.value)} />
            </FieldRow>
            <TextBlock
              id={`${ids}-dar`}
              label={t("job.descriptionAr")}
              value={fields.description_ar}
              onChange={(v) => set("description_ar", v)}
              dir="rtl"
              lang="ar"
              field="description_ar"
            />
            <Findings
              lang="ar"
              inclusive={checks?.inclusive_ar ?? []}
              sanity={checks?.sanity_ar ?? []}
              disabled={busy !== null}
              onAccept={accept}
            />
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" onClick={save} disabled={busy !== null || !jobDirty} data-save-job>
            {busy === "save" ? t("saving") : job ? t("save") : t("create")}
          </Button>
          <Button type="button" variant="outline" onClick={() => recheck()} disabled={busy !== null}>
            {t("checks.run")}
          </Button>
          {jobDirty && job && <span className="text-xs text-muted-foreground">{t("unsaved")}</span>}
        </div>
        <NoticeLine notice={jobNotice} data="job" />
        {stepUp && !rubric && <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />}
      </section>

      {/* ---------------------------------------------------------- rubric */}
      {canRubric && (
        <section aria-labelledby={`${ids}-rubric`} className="space-y-5 rounded-xl border bg-surface p-5" data-rubric>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id={`${ids}-rubric`} className="eyebrow">
              {t("rubric.title")}
            </h2>
            {locked && (
              <Badge variant={needsLock ? "outline" : "default"} data-rubric-locked={locked.version}>
                {needsLock ? t("rubric.changedSince", { version: locked.version }) : t("rubric.lockedBadge", { version: locked.version })}
              </Badge>
            )}
          </div>
          {!job ? (
            <p className="text-sm text-muted-foreground">{t("rubric.saveFirst")}</p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">{t("rubric.intro")}</p>
              <div className="flex flex-wrap gap-3">
                <Button
                  type="button"
                  variant={rubric ? "outline" : "default"}
                  onClick={draftRubric}
                  disabled={busy !== null || !job.description_en.trim() || jobDirty}
                  data-draft-rubric
                >
                  <Sparkles aria-hidden />
                  {busy === "rubricDraft" ? t("draft.working") : rubric ? t("rubric.redraft") : t("rubric.draft")}
                </Button>
                {!rubric && (
                  <Button type="button" variant="ghost" onClick={writeYourself} disabled={busy !== null}>
                    {t("rubric.writeYourself")}
                  </Button>
                )}
              </div>
              {(!job.description_en.trim() || jobDirty) && <p className="text-xs text-muted-foreground">{t("rubric.needsDescription")}</p>}
              {rubric && (
                <RubricForm
                  rubric={rubric}
                  onChange={(next) => {
                    setRubric(next);
                    setProblem(null);
                  }}
                  problem={problem}
                />
              )}
              <div className="flex flex-wrap items-center gap-3 border-t pt-4">
                {rubric && (
                  <Button type="button" variant="outline" onClick={saveRubric} disabled={busy !== null || !rubricDirty} data-save-rubric>
                    {busy === "rubricSave" ? t("saving") : t("rubric.save")}
                  </Button>
                )}
                {rubric && canLock && (
                  <Button type="button" onClick={lock} disabled={busy !== null || !needsLock} data-lock-rubric>
                    <Lock aria-hidden />
                    {busy === "lock" ? t("rubric.locking") : t("rubric.lock")}
                  </Button>
                )}
                {canPublish && (
                  <Button type="button" variant="signal" onClick={publish} disabled={busy !== null || !canPublishNow} data-publish-job>
                    <Send aria-hidden />
                    {busy === "publish" ? t("publish.working") : t("publish.button")}
                  </Button>
                )}
              </div>
              {canPublish && job.status === "draft" && !canPublishNow && (
                <p className="text-xs text-muted-foreground">{t("publish.needs")}</p>
              )}
              <NoticeLine notice={rubricNotice} data="rubric" />
              {stepUp && <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />}
            </>
          )}
        </section>
      )}
    </div>
  );
}

function NoticeLine({ notice, data }: { notice: Notice; data: string }) {
  if (!notice) return <div role="status" aria-live="polite" className="sr-only" />;
  return (
    <p
      role={notice.tone === "error" ? "alert" : "status"}
      aria-live="polite"
      data-notice={data}
      data-tone={notice.tone}
      className={cn(
        "rounded-md px-3 py-2 text-sm",
        notice.tone === "error" && "bg-destructive/10 text-destructive",
        notice.tone === "ok" && "bg-accent/50",
        notice.tone === "info" && "bg-signal/10",
      )}
    >
      {notice.text}
    </p>
  );
}

function TextBlock({
  id,
  label,
  value,
  onChange,
  dir,
  lang,
  field,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  dir: "ltr" | "rtl";
  lang: string;
  field: string;
}) {
  return (
    <FieldRow label={label} htmlFor={id}>
      <Textarea
        id={id}
        rows={12}
        maxLength={20_000}
        dir={dir}
        lang={lang}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-field={field}
      />
    </FieldRow>
  );
}

function Findings({
  lang,
  inclusive,
  sanity,
  disabled,
  onAccept,
}: {
  lang: "en" | "ar";
  inclusive: HireFinding[];
  sanity: HireFinding[];
  disabled: boolean;
  onAccept: (lang: "en" | "ar", finding: HireFinding, suggestion: string) => void;
}) {
  const t = useTranslations("HireJob");
  if (inclusive.length === 0 && sanity.length === 0) {
    return <p className="text-xs text-muted-foreground" data-checks-clear={lang}>{t("checks.clear")}</p>;
  }
  return (
    <div className="space-y-3">
      {inclusive.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("checks.inclusive")}</h3>
          <ul className="space-y-2">
            {inclusive.map((f, i) => (
              <li key={`${f.rule}-${f.start}-${i}`} className="rounded-md border px-3 py-2 text-sm" data-inclusive-finding={f.rule}>
                <p>
                  <mark className="rounded bg-signal/20 px-1">{f.match}</mark> <span className="text-muted-foreground">{f.message}</span>
                </p>
                {f.suggestions.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {f.suggestions.map((s) => (
                      <Button
                        key={s}
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={disabled}
                        onClick={() => onAccept(lang, f, s)}
                        data-accept-suggestion={s}
                      >
                        {t("checks.use", { word: s })}
                      </Button>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {sanity.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("checks.sanity")}</h3>
          <ul className="space-y-2">
            {sanity.map((f, i) => (
              <li key={`${f.rule}-${f.start}-${i}`} className="rounded-md border border-signal/40 px-3 py-2 text-sm" data-sanity-warning={f.rule}>
                {f.match && <span className="font-medium">{f.match}: </span>}
                {f.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ rubric form

function RubricForm({
  rubric,
  onChange,
  problem,
}: {
  rubric: RubricDraft;
  onChange: (next: RubricDraft) => void;
  problem: RubricProblem | null;
}) {
  const t = useTranslations("HireJob.rubric");
  const allIds = useMemo(() => [...rubric.must_haves, ...rubric.nice_to_haves].map((c) => c.id), [rubric]);

  const setGroup = (group: "must_haves" | "nice_to_haves", items: RubricCriterion[]) => onChange({ ...rubric, [group]: items });
  const setKnockouts = (items: RubricKnockout[]) => onChange({ ...rubric, knockout_questions: items });

  return (
    <div className="space-y-6">
      {(["must_haves", "nice_to_haves"] as const).map((group) => (
        <fieldset key={group} className="space-y-3" data-rubric-group={group}>
          <legend className="text-sm font-semibold">{t(group === "must_haves" ? "mustHaves" : "niceToHaves")}</legend>
          {rubric[group].length === 0 && <p className="text-xs text-muted-foreground">{t("none")}</p>}
          {rubric[group].map((c, index) => (
            <CriterionCard
              key={`${group}-${index}`}
              criterion={c}
              invalid={problem?.kind === "criterion" && problem.group === group && problem.index === index ? problem.field : null}
              onChange={(next) => setGroup(group, rubric[group].map((x, i) => (i === index ? next : x)))}
              onRemove={() => setGroup(group, rubric[group].filter((_, i) => i !== index))}
            />
          ))}
          <Button type="button" size="sm" variant="ghost" onClick={() => setGroup(group, [...rubric[group], newCriterion(allIds)])}>
            <Plus aria-hidden />
            {t(group === "must_haves" ? "addMust" : "addNice")}
          </Button>
        </fieldset>
      ))}
      <fieldset className="space-y-3" data-rubric-group="knockout_questions">
        <legend className="text-sm font-semibold">{t("knockouts")}</legend>
        <p className="text-xs text-muted-foreground">{t("knockoutsHint")}</p>
        {rubric.knockout_questions.map((k, index) => (
          <KnockoutCard
            key={`k-${index}`}
            knockout={k}
            invalid={problem?.kind === "knockout" && problem.index === index ? problem.field : null}
            onChange={(next) => setKnockouts(rubric.knockout_questions.map((x, i) => (i === index ? next : x)))}
            onRemove={() => setKnockouts(rubric.knockout_questions.filter((_, i) => i !== index))}
          />
        ))}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => setKnockouts([...rubric.knockout_questions, newKnockout(rubric.knockout_questions.map((k) => k.id))])}
        >
          <Plus aria-hidden />
          {t("addKnockout")}
        </Button>
      </fieldset>
      {problem && (problem.kind === "no_must_haves" || problem.kind === "too_many") && (
        <p role="alert" className="text-sm text-destructive">
          {t(problem.kind === "no_must_haves" ? "needMust" : "tooMany")}
        </p>
      )}
    </div>
  );
}

function CriterionCard({
  criterion: c,
  invalid,
  onChange,
  onRemove,
}: {
  criterion: RubricCriterion;
  invalid: string | null;
  onChange: (next: RubricCriterion) => void;
  onRemove: () => void;
}) {
  const t = useTranslations("HireJob.rubric");
  const id = useId();
  const anchors = c.anchors.length === SCALE_LEVELS ? c.anchors : Array.from({ length: SCALE_LEVELS }, (_, i) => c.anchors[i] ?? "");
  return (
    <div className="space-y-3 rounded-lg border p-4" data-criterion-editor={c.id}>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem_auto] sm:items-end">
        <FieldRow label={t("name")} htmlFor={`${id}-name`}>
          <Input
            id={`${id}-name`}
            value={c.name}
            maxLength={160}
            aria-invalid={invalid === "name" || invalid === "id" || undefined}
            onChange={(e) => onChange({ ...c, name: e.target.value })}
          />
        </FieldRow>
        <FieldRow label={t("weight")} htmlFor={`${id}-weight`}>
          <Input
            id={`${id}-weight`}
            type="number"
            min={1}
            max={10}
            step={1}
            dir="ltr"
            value={Number.isFinite(c.weight) ? c.weight : ""}
            aria-invalid={invalid === "weight" || undefined}
            onChange={(e) => onChange({ ...c, weight: e.target.value === "" ? Number.NaN : Number(e.target.value) })}
          />
        </FieldRow>
        <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label={t("remove", { name: c.name || c.id })}>
          <Trash2 aria-hidden />
        </Button>
      </div>
      <FieldRow label={t("description")} htmlFor={`${id}-desc`}>
        <Textarea
          id={`${id}-desc`}
          rows={2}
          maxLength={1200}
          value={c.description}
          aria-invalid={invalid === "description" || undefined}
          onChange={(e) => onChange({ ...c, description: e.target.value })}
        />
      </FieldRow>
      <FieldRow label={t("evidence")} htmlFor={`${id}-evidence`} hint={t("evidenceHint")}>
        <Textarea
          id={`${id}-evidence`}
          rows={2}
          maxLength={1200}
          value={c.evidence}
          aria-invalid={invalid === "evidence" || undefined}
          onChange={(e) => onChange({ ...c, evidence: e.target.value })}
        />
      </FieldRow>
      <details className="rounded-md border px-3 py-2" open={invalid === "anchors" || undefined}>
        <summary className="cursor-pointer text-sm font-medium">{t("anchors")}</summary>
        <p className="mt-2 text-xs text-muted-foreground">{t("anchorsHint")}</p>
        <div className="mt-2 space-y-2">
          {anchors.map((a, i) => (
            <FieldRow key={i} label={t("score", { score: i })} htmlFor={`${id}-a${i}`}>
              <Input
                id={`${id}-a${i}`}
                value={a}
                maxLength={300}
                aria-invalid={invalid === "anchors" || undefined}
                onChange={(e) => {
                  const next = [...anchors];
                  next[i] = e.target.value;
                  onChange({ ...c, anchors: next.some((x) => x.trim()) ? next : [] });
                }}
              />
            </FieldRow>
          ))}
        </div>
      </details>
    </div>
  );
}

function KnockoutCard({
  knockout: k,
  invalid,
  onChange,
  onRemove,
}: {
  knockout: RubricKnockout;
  invalid: string | null;
  onChange: (next: RubricKnockout) => void;
  onRemove: () => void;
}) {
  const t = useTranslations("HireJob.rubric");
  const id = useId();
  return (
    <div className="space-y-3 rounded-lg border p-4" data-knockout-editor={k.id}>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem_auto] sm:items-end">
        <FieldRow label={t("question")} htmlFor={`${id}-q`}>
          <Input
            id={`${id}-q`}
            value={k.question}
            maxLength={1200}
            aria-invalid={invalid === "question" || invalid === "id" || undefined}
            onChange={(e) => onChange({ ...k, question: e.target.value })}
          />
        </FieldRow>
        <FieldRow label={t("expected")} htmlFor={`${id}-e`}>
          <Select id={`${id}-e`} value={k.expected_answer} onChange={(e) => onChange({ ...k, expected_answer: e.target.value === "no" ? "no" : "yes" })}>
            <option value="yes">{t("yes")}</option>
            <option value="no">{t("no")}</option>
          </Select>
        </FieldRow>
        <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label={t("remove", { name: k.question || k.id })}>
          <Trash2 aria-hidden />
        </Button>
      </div>
      <FieldRow label={t("questionAr")} htmlFor={`${id}-qa`}>
        <Input id={`${id}-qa`} dir="rtl" lang="ar" value={k.question_ar} maxLength={1200} onChange={(e) => onChange({ ...k, question_ar: e.target.value })} />
      </FieldRow>
      <FieldRow label={t("reason")} htmlFor={`${id}-r`} hint={t("reasonHint")}>
        <Input
          id={`${id}-r`}
          value={k.reason}
          maxLength={1200}
          aria-invalid={invalid === "reason" || undefined}
          onChange={(e) => onChange({ ...k, reason: e.target.value })}
        />
      </FieldRow>
    </div>
  );
}

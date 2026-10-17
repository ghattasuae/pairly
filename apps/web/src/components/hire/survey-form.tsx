// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Loader2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Textarea } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import { answerSurvey, viewSurvey } from "@/lib/actions/survey";
import type { HireSurveyForm } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

type Ratings = { overall: number | null; communication: number | null; respect: number | null };

function Choices({
  label,
  options,
  value,
  onChange,
  render,
}: {
  label: string;
  options: readonly (string | number)[];
  value: string | number | null;
  onChange: (value: string | number) => void;
  render: (value: string | number) => string;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="font-medium">{label}</legend>
      <div role="radiogroup" aria-label={label} className={cn("grid gap-2", options.length === 5 ? "grid-cols-5" : "grid-cols-3")}>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={value === option}
            onClick={() => onChange(option)}
            className={cn(
              "min-h-12 rounded-lg border px-2 text-sm font-medium transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none",
              value === option ? "border-primary bg-primary/10" : "bg-surface hover:bg-accent",
            )}
          >
            {render(option)}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

/** The 5 questions, anonymous unless the candidate ticks "add my name". */
export function SurveyForm({ token, form }: { token: string; form: HireSurveyForm }) {
  const t = useTranslations("Survey");
  const tErrors = useTranslations("Errors");
  const ids = useId();
  const [ratings, setRatings] = useState<Ratings>({ overall: null, communication: null, respect: null });
  const [recommend, setRecommend] = useState<"yes" | "no" | "unsure" | null>(null);
  const [comment, setComment] = useState("");
  const [named, setNamed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const label = (id: string) => form.questions.find((q) => q.id === id)?.label ?? id;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (ratings.overall === null) return;
    setBusy(true);
    setError(null);
    const result = await answerSurvey(token, { ...ratings, overall: ratings.overall, recommend, comment }, named);
    setBusy(false);
    if (result.ok) setDone(true);
    else setError(result.code);
  }

  if (done) {
    return (
      <p role="status" className="flex items-center gap-2 rounded-xl border bg-surface p-6 text-lg font-medium" data-survey-done>
        <Check aria-hidden className="size-5 text-primary" />
        {t("thanks")}
      </p>
    );
  }
  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-6 pb-24" data-survey>
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">{form.company}</p>
        <h1 className="font-display text-2xl">{t("heading", { job: form.job_title })}</h1>
        <p className="text-sm text-muted-foreground">{t("anonymous")}</p>
      </header>
      {(["overall", "communication", "respect"] as const).map((key) => (
        <Choices
          key={key}
          label={label(key)}
          options={[1, 2, 3, 4, 5]}
          value={ratings[key]}
          onChange={(v) => setRatings((r) => ({ ...r, [key]: Number(v) }))}
          render={(v) => String(v)}
        />
      ))}
      <p className="-mt-4 text-xs text-muted-foreground">{t("scale")}</p>
      <Choices
        label={label("recommend")}
        options={["yes", "no", "unsure"]}
        value={recommend}
        onChange={(v) => setRecommend(v as "yes" | "no" | "unsure")}
        render={(v) => t(`recommend.${String(v)}` as "recommend.yes")}
      />
      <div className="space-y-2">
        <label htmlFor={`${ids}-comment`} className="font-medium">
          {label("comment")}
        </label>
        <Textarea id={`${ids}-comment`} maxLength={1000} value={comment} onChange={(e) => setComment(e.target.value)} />
      </div>
      <label className="flex items-start gap-2 text-sm">
        <Checkbox checked={named} onChange={(e) => setNamed(e.target.checked)} data-attach-name />
        <span>{t("attachName")}</span>
      </label>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error === "not_found" ? t("linkInvalid") : tErrors(errorKey(error) as "generic")}
        </p>
      )}
      <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 p-3 backdrop-blur sm:static sm:border-0 sm:bg-transparent sm:p-0">
        <div className="mx-auto max-w-3xl">
          <Button type="submit" size="lg" className="w-full sm:w-auto" disabled={busy || ratings.overall === null} data-submit-survey>
            {busy && <Loader2 aria-hidden className="animate-spin" />}
            {t("submit")}
          </Button>
        </div>
      </div>
    </form>
  );
}

/** `/<locale>/survey#<link>`: the link stays in the fragment, never in a server log. */
export function SurveyApp() {
  const t = useTranslations("Survey");
  const tErrors = useTranslations("Errors");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const [state, setState] = useState<{ kind: "loading" } | { kind: "form"; token: string; form: HireSurveyForm } | { kind: "error"; code: string }>({
    kind: "loading",
  });
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.replace(/^#/, "");
    if (token) window.history.replaceState(null, "", window.location.pathname);
    void (async () => {
      const result = await viewSurvey(token, locale);
      setState(result.ok ? { kind: "form", token, form: result.form } : { kind: "error", code: result.code === "not_found" ? "link" : result.code });
    })();
  }, [locale]);

  if (state.kind === "loading") {
    return (
      <p role="status" className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {t("opening")}
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="rounded-xl border bg-surface p-5 font-medium">
        {state.code === "link" ? t("linkInvalid") : tErrors(errorKey(state.code) as "generic")}
      </p>
    );
  }
  return <SurveyForm token={state.token} form={state.form} />;
}

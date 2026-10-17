// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Loader2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { errorKey } from "@/lib/action-result";
import { loadReference, openReference, saveReference, type RefereeAnswers } from "@/lib/actions/referee";
import type { HireRefereePage } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

type Answers = Record<string, string | number | null>;

function filled(value: string | number | null | undefined): boolean {
  return typeof value === "number" || (typeof value === "string" && value.trim() !== "");
}

/** One question of the reference form, by kind: choice, line, month, text or rating. */
function Question({
  q,
  value,
  onChange,
  inputId,
}: {
  q: HireRefereePage["questions"][number];
  value: string | number | null | undefined;
  onChange: (value: string | number | null) => void;
  inputId: string;
}) {
  const t = useTranslations("Referee");
  if (q.kind === "choice" || q.kind === "rating") {
    const options = q.kind === "rating" ? ["1", "2", "3", "4", "5"] : q.options;
    return (
      <fieldset className="space-y-2" data-question={q.id}>
        <legend className="font-medium">
          {q.label}
          {q.required && <span aria-hidden> *</span>}
        </legend>
        <div role="radiogroup" aria-label={q.label} className={cn("grid gap-2", q.kind === "rating" ? "grid-cols-5" : "grid-cols-2 sm:grid-cols-3")}>
          {options.map((option) => {
            const v = q.kind === "rating" ? Number(option) : option;
            const on = value === v;
            return (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={on}
                data-option={option}
                onClick={() => onChange(v)}
                className={cn(
                  "min-h-12 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none",
                  on ? "border-primary bg-primary/10" : "bg-surface hover:bg-accent",
                )}
              >
                {q.kind === "rating" ? option : t(`option.${option}` as "option.yes")}
              </button>
            );
          })}
        </div>
      </fieldset>
    );
  }
  const label = (
    <label htmlFor={inputId} className="font-medium">
      {q.label}
      {q.required && <span aria-hidden> *</span>}
    </label>
  );
  return (
    <div className="space-y-2" data-question={q.id}>
      {label}
      {q.kind === "text" ? (
        <Textarea id={inputId} maxLength={1000} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <Input
          id={inputId}
          type={q.kind === "month" ? "month" : "text"}
          maxLength={120}
          dir={q.kind === "month" ? "ltr" : undefined}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}

/** The referee's form: clear context, progress saving, then a confirmation. */
export function RefereeForm({ initial }: { initial: HireRefereePage }) {
  const t = useTranslations("Referee");
  const tErrors = useTranslations("Errors");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const ids = useId();
  const [page, setPage] = useState(initial);
  const [answers, setAnswers] = useState<Answers>(initial.answers as Answers);
  const [busy, setBusy] = useState<"save" | "submit" | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const required = page.questions.filter((q) => q.required);
  const done = required.filter((q) => filled(answers[q.id])).length;
  const complete = done === required.length;

  async function save(submit: boolean) {
    setBusy(submit ? "submit" : "save");
    setError(null);
    setSaved(false);
    const clean: RefereeAnswers = Object.fromEntries(
      Object.entries(answers).map(([k, v]) => [k, typeof v === "string" && v.trim() === "" ? null : v]),
    );
    const result = await saveReference(clean, submit, locale);
    setBusy(null);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setPage(result.page);
    if (!submit) setSaved(true);
  }

  if (page.status === "submitted") {
    return (
      <section className="space-y-3 rounded-xl border bg-surface p-6" data-reference-submitted>
        <p role="status" className="flex items-center gap-2 text-lg font-medium">
          <Check aria-hidden className="size-5 text-primary" />
          {t("thanks", { name: page.referee_name })}
        </p>
        <p className="text-sm text-muted-foreground">{t("thanksBody", { company: page.company })}</p>
      </section>
    );
  }

  return (
    <div className="space-y-6 pb-24" data-reference-form>
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">{page.company}</p>
        <h1 className="font-display text-2xl">{t("heading", { candidate: page.candidate_name })}</h1>
        <p className="text-sm text-muted-foreground">
          {t("context", { name: page.referee_name, candidate: page.candidate_name, job: page.job_title, company: page.company })}
        </p>
        <p className="text-sm text-muted-foreground">{t("privacy")}</p>
        <p className="text-sm font-medium" data-progress>
          {t("progress", { done, total: required.length })}
        </p>
      </header>
      <form
        className="space-y-6"
        onSubmit={(e) => {
          e.preventDefault();
          void save(true);
        }}
      >
        {page.questions.map((q) => (
          <Question
            key={q.id}
            q={q}
            inputId={`${ids}-${q.id}`}
            value={answers[q.id]}
            onChange={(value) => {
              setSaved(false);
              setAnswers((a) => ({ ...a, [q.id]: value }));
            }}
          />
        ))}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error === "invalid_input" ? t("checkAnswers") : tErrors(errorKey(error) as "generic")}
          </p>
        )}
        {saved && (
          <p role="status" className="text-sm text-muted-foreground" data-saved>
            {t("saved")}
          </p>
        )}
        <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 p-3 backdrop-blur sm:static sm:border-0 sm:bg-transparent sm:p-0">
          <div className="mx-auto flex max-w-3xl flex-wrap gap-2">
            <Button type="submit" size="lg" className="flex-1 sm:flex-none" disabled={!complete || busy !== null} data-submit-reference>
              {busy === "submit" && <Loader2 aria-hidden className="animate-spin" />}
              {t("submit")}
            </Button>
            <Button type="button" size="lg" variant="outline" disabled={busy !== null} onClick={() => void save(false)} data-save-reference>
              {busy === "save" && <Loader2 aria-hidden className="animate-spin" />}
              {t("save")}
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}

/**
 * `/<locale>/reference#<link>`: the referee's signed link. The link stays in the fragment
 * (never sent to a server log); the page posts it once, the session goes into an
 * HTTP-only cookie, and the form opens with any saved progress.
 */
export function RefereeApp() {
  const t = useTranslations("Referee");
  const tErrors = useTranslations("Errors");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const [state, setState] = useState<{ kind: "loading" } | { kind: "page"; page: HireRefereePage } | { kind: "error"; code: string }>({
    kind: "loading",
  });
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.replace(/^#/, "");
    if (token) window.history.replaceState(null, "", window.location.pathname);
    void (async () => {
      if (token.startsWith("rr1.")) {
        const opened = await openReference(token);
        if (!opened.ok) {
          // A used link may still have a live session in this browser.
          const existing = await loadReference(locale);
          setState(existing.ok ? { kind: "page", page: existing.page } : { kind: "error", code: "link" });
          return;
        }
      }
      const loaded = await loadReference(locale);
      setState(loaded.ok ? { kind: "page", page: loaded.page } : { kind: "error", code: loaded.code === "not_signed_in" ? "link" : loaded.code });
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
      <div role="alert" className="space-y-2 rounded-xl border bg-surface p-5">
        <p className="font-medium">{state.code === "link" ? t("linkInvalid") : tErrors(errorKey(state.code) as "generic")}</p>
        {state.code === "link" && <p className="text-sm text-muted-foreground">{t("linkHelp")}</p>}
      </div>
    );
  }
  return <RefereeForm initial={state.page} />;
}

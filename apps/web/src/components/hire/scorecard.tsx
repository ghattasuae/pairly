// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Clock, Loader2, Mic, MicOff } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";

import { InterviewKit } from "@/components/hire/interview-kit";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import {
  loadScorecard,
  openScorecard,
  saveScorecard,
  snoozeFromLink,
  snoozeScorecard,
  type ScorecardInput,
} from "@/lib/actions/scorecard";
import type { HireScorecardPage } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

const RECOMMENDATIONS = ["strong_no", "no", "yes", "strong_yes"] as const;
type Recommendation = (typeof RECOMMENDATIONS)[number];

interface SpeechResultEvent {
  results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
  resultIndex: number;
}
interface Recognizer {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: SpeechResultEvent) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

function speechRecognizer(): (new () => Recognizer) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: new () => Recognizer; webkitSpeechRecognition?: new () => Recognizer };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const noSubscribe = () => () => {};

/** Voice to text for the notes, where the browser offers it (progressive enhancement). */
function VoiceButton({ onText }: { onText: (text: string) => void }) {
  const t = useTranslations("Scorecard");
  const locale = useLocale();
  const [listening, setListening] = useState(false);
  const supported = useSyncExternalStore(
    noSubscribe,
    () => speechRecognizer() !== null,
    () => false,
  );
  const recognizer = useRef<Recognizer | null>(null);
  if (!supported) return null;
  function toggle() {
    if (listening) {
      recognizer.current?.stop();
      return;
    }
    const Ctor = speechRecognizer();
    if (!Ctor) return;
    const r = new Ctor();
    r.lang = locale === "ar" ? "ar-AE" : "en-GB";
    r.interimResults = false;
    r.continuous = true;
    r.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        if (result?.isFinal && result[0]) onText(result[0].transcript.trim());
      }
    };
    r.onend = () => setListening(false);
    recognizer.current = r;
    setListening(true);
    r.start();
  }
  return (
    <Button type="button" size="sm" variant="outline" onClick={toggle} aria-pressed={listening}>
      {listening ? <MicOff aria-hidden /> : <Mic aria-hidden />}
      {listening ? t("stopDictation") : t("dictate")}
    </Button>
  );
}

/** The scorecard itself (brief 5.3): 3 to 5 criteria rated 1 to 4 with anchors, short
 * notes, an overall recommendation. Others' feedback appears only after you submit. */
export function ScorecardForm({ initial }: { initial: HireScorecardPage }) {
  const t = useTranslations("Scorecard");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const ids = useId();
  const [page, setPage] = useState(initial);
  const card = page.scorecard;
  const [ratings, setRatings] = useState<Record<string, number>>(card.ratings ?? {});
  const [notes, setNotes] = useState(card.notes ?? "");
  const [recommendation, setRecommendation] = useState<Recommendation | null>(
    (card.recommendation as Recommendation | null | undefined) ?? null,
  );
  const [busy, setBusy] = useState<"save" | "submit" | "later" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [snoozed, setSnoozed] = useState<string | null>(null);
  const submitted = card.status === "submitted";
  const complete = card.criteria.every((c) => ratings[c.id]) && recommendation !== null;

  function input(): ScorecardInput {
    return { ratings, notes, recommendation };
  }

  async function save(submit: boolean) {
    setBusy(submit ? "submit" : "save");
    setError(null);
    const result = await saveScorecard(input(), submit);
    setBusy(null);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setPage(result.page);
  }

  async function later() {
    setBusy("later");
    const result = await snoozeScorecard();
    setBusy(null);
    if (result.ok) setSnoozed(result.until);
    else setError(result.code);
  }

  return (
    <div className="space-y-6 pb-24" data-scorecard data-status={card.status}>
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">
          {card.job_title} · {card.stage_name}
        </p>
        <h1 className="font-display text-2xl" data-candidate-label>
          {card.candidate_label}
        </h1>
        {card.starts_at && (
          <p className="text-sm text-muted-foreground">
            {format.dateTime(new Date(card.starts_at), { dateStyle: "medium", timeStyle: "short" })}
          </p>
        )}
        {!submitted && card.due_at && (
          <p className={cn("flex items-center gap-1.5 text-sm", card.sla_state === "overdue" ? "text-danger-ink" : "text-muted-foreground")}>
            <Clock aria-hidden className="size-4" />
            {card.sla_state === "overdue"
              ? t("overdue")
              : t("due", { when: format.dateTime(new Date(card.due_at), { dateStyle: "medium", timeStyle: "short" }) })}
          </p>
        )}
      </header>

      {page.kit && (
        <details className="rounded-xl border bg-surface p-4">
          <summary className="cursor-pointer font-medium">{t("kit")}</summary>
          <div className="mt-3">
            <InterviewKit kit={page.kit} />
          </div>
        </details>
      )}

      {submitted ? (
        <section className="space-y-4" aria-labelledby={`${ids}-done`}>
          <p id={`${ids}-done`} role="status" className="flex items-center gap-2 rounded-xl border bg-surface p-4 font-medium" data-submitted>
            <Check aria-hidden className="size-5 text-primary" />
            {t("submitted")}
          </p>
          <h2 className="font-display text-lg">{t("othersTitle")}</h2>
          {(card.others ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("noOthers")}</p>
          ) : (
            <ul className="space-y-3" data-others>
              {(card.others ?? []).map((o) => (
                <li key={o.interviewer_id} className="space-y-2 rounded-xl border bg-surface p-4 text-sm">
                  <p className="font-medium">
                    {o.interviewer_name} · {t(`recommendation.${o.recommendation}` as "recommendation.yes")}
                  </p>
                  <ul className="grid gap-1 sm:grid-cols-2">
                    {card.criteria.map((c) => (
                      <li key={c.id}>
                        {c.name}: <span className="font-medium">{o.ratings[c.id] ?? "-"}</span> / 4
                      </li>
                    ))}
                  </ul>
                  {o.notes && <p className="text-muted-foreground">{o.notes}</p>}
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : (
        <form
          className="space-y-6"
          onSubmit={(e) => {
            e.preventDefault();
            void save(true);
          }}
        >
          <p className="text-sm text-muted-foreground">
            {t("hiddenNote", { count: card.others_submitted })}
          </p>
          {card.criteria.map((c, n) => (
            <fieldset key={c.id} className="space-y-2" data-criterion={c.id}>
              <legend className="font-medium">
                {n + 1}. {c.name}
              </legend>
              {c.question && <p className="text-sm text-muted-foreground">{c.question}</p>}
              <div role="radiogroup" aria-label={c.name} className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {[1, 2, 3, 4].map((value) => {
                  const on = ratings[c.id] === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      data-rating={value}
                      onClick={() => setRatings((r) => ({ ...r, [c.id]: value }))}
                      className={cn(
                        "flex min-h-16 flex-col items-start gap-1 rounded-lg border p-2 text-start text-xs leading-snug transition-colors",
                        on ? "border-primary bg-info-soft ring-1 ring-primary" : "border-input bg-surface hover:bg-accent",
                      )}
                    >
                      <span className="text-base font-semibold">{value}</span>
                      <span className="text-muted-foreground">{c.anchors[value - 1]}</span>
                    </button>
                  );
                })}
              </div>
            </fieldset>
          ))}
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <label htmlFor={`${ids}-notes`} className="font-medium">
                {t("notes")}
              </label>
              <VoiceButton onText={(text) => setNotes((v) => (v ? `${v} ${text}` : text).slice(0, 1000))} />
            </div>
            <Textarea
              id={`${ids}-notes`}
              maxLength={1000}
              value={notes}
              placeholder={t("notesHint")}
              onChange={(e) => setNotes(e.target.value)}
              data-notes
            />
          </div>
          <fieldset className="space-y-2">
            <legend className="font-medium">{t("overall")}</legend>
            <div role="radiogroup" aria-label={t("overall")} className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {RECOMMENDATIONS.map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={recommendation === value}
                  data-recommendation={value}
                  onClick={() => setRecommendation(value)}
                  className={cn(
                    "min-h-12 rounded-lg border px-3 text-sm font-medium transition-colors",
                    recommendation === value ? "border-primary bg-info-soft ring-1 ring-primary" : "border-input bg-surface hover:bg-accent",
                  )}
                >
                  {t(`recommendation.${value}`)}
                </button>
              ))}
            </div>
          </fieldset>
          {error && (
            <p role="alert" className="text-sm text-danger-ink">
              {tErrors(errorKey(error) as "generic")}
            </p>
          )}
          {snoozed && (
            <p role="status" className="text-sm text-muted-foreground">
              {t("snoozed", { when: format.dateTime(new Date(snoozed), { timeStyle: "short" }) })}
            </p>
          )}
          <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 p-3 backdrop-blur sm:static sm:border-0 sm:bg-transparent sm:p-0">
            <div className="mx-auto flex max-w-3xl flex-wrap gap-2">
              <Button type="submit" size="lg" className="flex-1 sm:flex-none" disabled={!complete || busy !== null} data-submit-scorecard>
                {busy === "submit" && <Loader2 aria-hidden className="animate-spin" />}
                {t("submit")}
              </Button>
              <Button type="button" size="lg" variant="outline" disabled={busy !== null} onClick={() => void save(false)}>
                {t("saveDraft")}
              </Button>
              <Button type="button" size="lg" variant="ghost" disabled={busy !== null} onClick={() => void later()} data-remind-later>
                {t("later")}
              </Button>
            </div>
          </div>
        </form>
      )}
    </div>
  );
}

/**
 * `/<locale>/scorecard#<link>`: the one-click link from the briefing, the calendar
 * invitation or a reminder. The link stays in the fragment (never sent to a server log);
 * the page posts it once (the one login step), the session goes into an HTTP-only
 * cookie, and the scorecard opens. A "remind me later" link snoozes the reminders.
 */
export function ScorecardApp() {
  const t = useTranslations("Scorecard");
  const tErrors = useTranslations("Errors");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const format = useFormatter();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "page"; page: HireScorecardPage }
    | { kind: "snoozed"; until: string }
    | { kind: "error"; code: string }
  >({ kind: "loading" });
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.replace(/^#/, "");
    if (token) window.history.replaceState(null, "", window.location.pathname);
    void (async () => {
      if (token.startsWith("sl1.")) {
        const result = await snoozeFromLink(token);
        setState(result.ok ? { kind: "snoozed", until: result.until } : { kind: "error", code: "link" });
        return;
      }
      if (token.startsWith("sk1.")) {
        const opened = await openScorecard(token);
        if (!opened.ok) {
          // A used link may still have a live session in this browser.
          const existing = await loadScorecard(locale);
          setState(existing.ok ? { kind: "page", page: existing.page } : { kind: "error", code: "link" });
          return;
        }
      }
      const loaded = await loadScorecard(locale);
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
  if (state.kind === "snoozed") {
    return (
      <p role="status" className="rounded-xl border bg-surface p-5" data-snoozed>
        {t("snoozed", { when: format.dateTime(new Date(state.until), { timeStyle: "short" }) })}
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
  return <ScorecardForm initial={state.page} />;
}

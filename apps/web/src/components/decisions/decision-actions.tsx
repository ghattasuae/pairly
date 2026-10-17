// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { CircleAlert, CircleCheck, CircleX, History, Loader2, MessageSquareWarning } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { FormError } from "@/components/auth/auth-card";
import { isRedacted, Redacted } from "@/components/data/json-view";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, Textarea } from "@/components/ui/form-controls";
import { useToast } from "@/components/ui/toaster";
import { fileAppeal, replayDecision, resolveAppeal } from "@/lib/actions/decisions";
import { errorKey } from "@/lib/action-result";
import type { Appeal, AppealOutcome, Replay, RequesterType } from "@/lib/api/types";
import { cn } from "@/lib/utils";

function shown(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Replay (brief 2.3.4): re-run the decision with the same inputs and versions, show the difference. */
export function ReplayPanel({
  decisionId,
  initial,
  canReplay,
  totpEnabled,
  replay = replayDecision,
}: {
  decisionId: string;
  initial: Replay[];
  canReplay: boolean;
  totpEnabled: boolean;
  replay?: typeof replayDecision;
}) {
  const t = useTranslations("Decision.replay");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const locale = useLocale();
  const [replays, setReplays] = useState(initial);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorId = useId();

  const run = async () => {
    setPending(true);
    setError(null);
    try {
      const result = await replay(decisionId, "recorded");
      if (result.ok) setReplays((all) => [{ ...result.replay, created_at: new Date().toISOString() }, ...all]);
      else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  if (!canReplay && replays.length === 0) return null;

  return (
    <section aria-labelledby="replay-heading" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="replay-heading" className="eyebrow">
          {t("title")}
        </h2>
        {canReplay && (
          <Button size="sm" variant="outline" onClick={run} disabled={pending}>
            {pending ? <Loader2 className="animate-spin" aria-hidden /> : <History aria-hidden />}
            {pending ? t("running") : t("run")}
          </Button>
        )}
      </div>
      <p className="text-sm text-muted-foreground">{t("body")}</p>
      {error === "second_factor_required" ? (
        <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/${locale}/decisions/${decisionId}`} />
      ) : (
        error && <FormError id={errorId}>{tErrors(errorKey(error) as "generic")}</FormError>
      )}
      {replays.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("none")}</p>
      ) : (
        <ol className="space-y-3">
          {replays.map((r) => {
            const Icon = r.status === "match" ? CircleCheck : r.status === "differs" ? CircleAlert : CircleX;
            return (
              <li key={r.id} className="space-y-2 rounded-xl border bg-surface p-4 text-sm">
                <p className="flex flex-wrap items-center gap-2">
                  <Icon
                    className={cn(
                      "size-4",
                      r.status === "match" ? "text-success-ink" : r.status === "differs" ? "text-signal-foreground dark:text-signal" : "text-danger-ink",
                    )}
                    aria-hidden
                  />
                  <span className="font-medium">{t(`status.${(["match", "differs", "failed"] as const).find((s) => s === r.status) ?? "failed"}`)}</span>
                  <Badge variant="outline">{t(`mode.${r.mode === "live" ? "live" : "recorded"}`)}</Badge>
                  {r.deterministic && <Badge variant="outline">{t("deterministic")}</Badge>}
                  {r.created_at && (
                    <span className="text-muted-foreground">
                      {format.dateTime(new Date(r.created_at), { dateStyle: "medium", timeStyle: "short" })}
                    </span>
                  )}
                </p>
                {r.reason && <p className="text-muted-foreground">{r.reason}</p>}
                {r.diff.length > 0 && (
                  <div className="relative overflow-x-auto rounded-lg border">
                    <table className="w-full text-xs">
                      <caption className="sr-only">{t("diffCaption")}</caption>
                      <thead>
                        <tr className="border-b text-muted-foreground">
                          <th scope="col" className="px-3 py-2 text-start font-medium">{t("path")}</th>
                          <th scope="col" className="px-3 py-2 text-start font-medium">{t("original")}</th>
                          <th scope="col" className="px-3 py-2 text-start font-medium">{t("replayed")}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y font-mono" dir="ltr">
                        {r.diff.slice(0, 100).map((d, i) => (
                          <tr key={i}>
                            <td className="px-3 py-1.5 break-all">{shown(d.path)}</td>
                            <td className="px-3 py-1.5 break-all text-danger-ink">{shown(d.original)}</td>
                            <td className="px-3 py-1.5 break-all text-success-ink">{shown(d.replayed)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {r.diff.some((d) => d.fingerprinted === true) && (
                      <p className="border-t px-3 py-2 text-xs text-muted-foreground">{t("fingerprinted")}</p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/** Appeals (brief 2.3.3): who contested the decision, the written outcome, file and resolve. */
export function AppealsPanel({
  decisionId,
  initial,
  canFile,
  canResolve,
  totpEnabled,
  file = fileAppeal,
  resolve = resolveAppeal,
}: {
  decisionId: string;
  initial: Appeal[];
  canFile: boolean;
  canResolve: boolean;
  totpEnabled: boolean;
  file?: typeof fileAppeal;
  resolve?: typeof resolveAppeal;
}) {
  const t = useTranslations("Decision.appeals");
  const format = useFormatter();
  const toast = useToast();
  const [appeals, setAppeals] = useState(initial);

  const replace = (appeal: Appeal) =>
    setAppeals((all) => (all.some((a) => a.id === appeal.id) ? all.map((a) => (a.id === appeal.id ? appeal : a)) : [appeal, ...all]));

  return (
    <section aria-labelledby="appeals-heading" className="space-y-4">
      <h2 id="appeals-heading" className="eyebrow">
        {t("title")}
      </h2>
      {appeals.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="space-y-3">
          {appeals.map((appeal) => (
            <li key={appeal.id} className="space-y-3 rounded-xl border bg-surface p-4 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <MessageSquareWarning className="size-4 text-primary" aria-hidden />
                <Badge variant={appeal.status === "open" ? "signal" : "outline"}>
                  {t(`status.${appeal.status === "open" ? "open" : "resolved"}`)}
                </Badge>
                <span className="text-muted-foreground">
                  {t(`requester.${(["candidate", "vendor", "staff"] as const).find((r) => r === appeal.requester_type) ?? "staff"}`)} ·{" "}
                  <bdi className="font-mono text-xs">{appeal.requester_id}</bdi>
                </span>
                <span className="text-muted-foreground">
                  · {format.dateTime(new Date(appeal.created_at), { dateStyle: "medium" })}
                </span>
                {appeal.status === "open" && (
                  <span className="text-muted-foreground">
                    · {t("due", { when: format.relativeTime(new Date(appeal.sla_due_at), new Date()) })}
                  </span>
                )}
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{t("reason")}</p>
                {isRedacted(appeal.reason) ? (
                  <Redacted />
                ) : (
                  <p className="whitespace-pre-wrap">
                    <bdi>{typeof appeal.reason === "string" ? appeal.reason : ""}</bdi>
                  </p>
                )}
              </div>
              {appeal.status !== "open" && (
                <div>
                  <p className="text-xs text-muted-foreground">
                    {t("outcomeLabel")}:{" "}
                    {appeal.outcome
                      ? t(`outcome.${(["upheld", "overturned", "partially_overturned"] as const).find((o) => o === appeal.outcome) ?? "upheld"}`)
                      : "-"}
                  </p>
                  {isRedacted(appeal.outcome_text) ? (
                    <Redacted />
                  ) : (
                    typeof appeal.outcome_text === "string" && (
                      <p className="whitespace-pre-wrap">
                        <bdi>{appeal.outcome_text}</bdi>
                      </p>
                    )
                  )}
                </div>
              )}
              {appeal.status === "open" && canResolve && (
                <ResolveForm
                  appeal={appeal}
                  decisionId={decisionId}
                  totpEnabled={totpEnabled}
                  resolve={resolve}
                  onDone={(updated) => {
                    replace(updated);
                    toast.show({ tone: "success", title: t("resolved") });
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {canFile && (
        <FileForm
          decisionId={decisionId}
          totpEnabled={totpEnabled}
          file={file}
          onDone={(appeal) => {
            replace(appeal);
            toast.show({ tone: "success", title: t("filed") });
          }}
        />
      )}
    </section>
  );
}

function FileForm({
  decisionId,
  totpEnabled,
  file,
  onDone,
}: {
  decisionId: string;
  totpEnabled: boolean;
  file: typeof fileAppeal;
  onDone: (appeal: Appeal) => void;
}) {
  const t = useTranslations("Decision.appeals");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const ids = { type: useId(), who: useId(), reason: useId(), error: useId() };
  const [requesterType, setRequesterType] = useState<RequesterType>("staff");
  const [requesterId, setRequesterId] = useState("");
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = requesterId.trim().length > 0 && reason.trim().length > 0;

  const submit = async () => {
    setPending(true);
    setError(null);
    try {
      const result = await file(decisionId, { requesterType, requesterId, reason });
      if (result.ok) {
        onDone(result.appeal);
        setRequesterId("");
        setReason("");
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) void submit();
      }}
      className="space-y-4 rounded-xl border bg-surface p-5"
    >
      <div className="space-y-1">
        <h3 className="font-display text-xl">{t("fileTitle")}</h3>
        <p className="text-sm text-muted-foreground">{t("fileBody")}</p>
      </div>
      <fieldset disabled={pending} className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <label htmlFor={ids.type} className="text-sm font-medium">
            {t("requesterType")}
          </label>
          <Select id={ids.type} value={requesterType} onChange={(e) => setRequesterType(e.target.value as RequesterType)}>
            {(["staff", "candidate", "vendor"] as const).map((r) => (
              <option key={r} value={r}>
                {t(`requester.${r}`)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <label htmlFor={ids.who} className="text-sm font-medium">
            {t("requesterId")}
          </label>
          <Input id={ids.who} dir="ltr" maxLength={128} value={requesterId} onChange={(e) => setRequesterId(e.target.value)} />
        </div>
        <div className="space-y-2 sm:col-span-2">
          <label htmlFor={ids.reason} className="text-sm font-medium">
            {t("reason")}
          </label>
          <Textarea id={ids.reason} maxLength={4000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </fieldset>
      {error === "second_factor_required" ? (
        <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/${locale}/decisions/${decisionId}`} />
      ) : (
        error && <FormError id={ids.error}>{tErrors(errorKey(error) as "generic")}</FormError>
      )}
      <Button type="submit" disabled={pending || !ready}>
        {pending ? t("filing") : t("file")}
      </Button>
    </form>
  );
}

function ResolveForm({
  appeal,
  decisionId,
  totpEnabled,
  resolve,
  onDone,
}: {
  appeal: Appeal;
  decisionId: string;
  totpEnabled: boolean;
  resolve: typeof resolveAppeal;
  onDone: (appeal: Appeal) => void;
}) {
  const t = useTranslations("Decision.appeals");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const ids = { outcome: useId(), text: useId(), error: useId() };
  const [outcome, setOutcome] = useState<AppealOutcome>("upheld");
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setPending(true);
    setError(null);
    try {
      const result = await resolve({ appealId: appeal.id, outcome, outcomeText: text });
      if (result.ok) onDone(result.appeal);
      else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (text.trim()) void submit();
      }}
      className="space-y-3 border-t pt-3"
    >
      <p className="font-medium">{t("resolveTitle")}</p>
      <fieldset disabled={pending} className="grid gap-3 sm:grid-cols-[14rem_1fr]">
        <div className="space-y-2">
          <label htmlFor={ids.outcome} className="text-sm">
            {t("outcomeLabel")}
          </label>
          <Select id={ids.outcome} value={outcome} onChange={(e) => setOutcome(e.target.value as AppealOutcome)}>
            {(["upheld", "partially_overturned", "overturned"] as const).map((o) => (
              <option key={o} value={o}>
                {t(`outcome.${o}`)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <label htmlFor={ids.text} className="text-sm">
            {t("outcomeText")}
          </label>
          <Textarea id={ids.text} required maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} className="min-h-20" />
        </div>
      </fieldset>
      {error === "second_factor_required" ? (
        <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/${locale}/decisions/${decisionId}`} compact />
      ) : (
        error && <FormError id={ids.error}>{tErrors(errorKey(error) as "generic")}</FormError>
      )}
      <Button type="submit" size="sm" disabled={pending || !text.trim()}>
        {pending ? t("resolving") : t("resolve")}
      </Button>
    </form>
  );
}

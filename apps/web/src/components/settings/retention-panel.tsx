// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2, Lock, LockOpen, Pause, Play } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import {
  placeRetentionHold,
  releaseRetentionHold,
  setRetentionCap,
  setRetentionPaused,
  updateRetentionRule,
} from "@/lib/actions/settings";
import type { ActionResult } from "@/lib/action-result";
import type { RetentionHold, RetentionRuleSetting, RetentionScheduleItem, RetentionSettings } from "@/lib/api/types";

import { ActionError } from "./admin-panels";

const KNOWN_RULES = new Set([
  "hire.candidates.closed",
  "hire.checklist.documents",
  "idempotency_keys.expired",
  "inbound_events.seen",
  "retention_reports.old",
  "webhook_deliveries.finished",
  "webhook_outbox.dispatched",
]);

/**
 * Retention settings and the deletion preview. Each rule is on or off with its period;
 * every subject a rule makes due is listed here for seven days before it may be deleted,
 * with the date, and an admin can place a legal hold that keeps it until released, or
 * pause a whole rule (nothing of it is deleted until resumed; resuming starts a new
 * preview).
 */
export function RetentionPanel({
  initial,
  schedule: initialSchedule,
  holds: initialHolds,
  canManage,
  totpEnabled,
  update = updateRetentionRule,
  setCap = setRetentionCap,
  hold = placeRetentionHold,
  release = releaseRetentionHold,
  pause = setRetentionPaused,
}: {
  initial: RetentionSettings;
  schedule: RetentionScheduleItem[];
  holds: RetentionHold[];
  canManage: boolean;
  totpEnabled: boolean;
  update?: typeof updateRetentionRule;
  setCap?: typeof setRetentionCap;
  hold?: typeof placeRetentionHold;
  release?: typeof releaseRetentionHold;
  pause?: typeof setRetentionPaused;
}) {
  const t = useTranslations("RetentionSettings");
  const locale = useLocale();
  const format = useFormatter();
  const toast = useToast();
  const ids = useId();
  const [settings, setSettings] = useState(initial);
  const [schedule, setSchedule] = useState(initialSchedule);
  const [holds, setHolds] = useState(initialHolds);
  const [cap, setCapValue] = useState(String(initial.daily_cap));
  const [periods, setPeriods] = useState<Record<string, string>>(() =>
    Object.fromEntries(initial.rules.map((r) => [r.key, String(r.period ? r.period.value : r.days)])),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [holdFor, setHoldFor] = useState<RetentionScheduleItem | null>(null);
  const [reason, setReason] = useState("");
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });
  const ruleName = (key: string) => (KNOWN_RULES.has(key) ? t(`rule.${key.replaceAll(".", "_")}` as "rule.hire_candidates_closed") : key);
  const heldIds = new Set(holds.map((h) => `${h.subject_type}:${h.subject_id}`));

  const run = async <T extends object>(key: string, action: () => Promise<ActionResult<T>>, done: (value: T) => void) => {
    setBusy(key);
    setError(null);
    try {
      const result = await action();
      if (result.ok) {
        done(result as T);
        toast.show({ tone: "success", title: t("saved") });
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setBusy(null);
  };

  const saveRule = (rule: RetentionRuleSetting, change: { enabled?: boolean; period?: number; days?: number }) =>
    run<{ settings: RetentionSettings }>(rule.key, () => update({ rule: rule.key, ...change }), ({ settings: next }) => {
      setSettings(next);
      if (change.enabled === false) setSchedule((all) => all.filter((i) => i.rule !== rule.key));
    });

  const togglePause = (rule: RetentionRuleSetting) =>
    run<{ settings: RetentionSettings; schedule: RetentionScheduleItem[] | null }>(
      rule.key,
      () => pause(rule.key, !rule.paused),
      ({ settings: next, schedule: listed }) => {
        setSettings(next);
        if (listed) setSchedule(listed);
      },
    );
  const pausedRules = new Set(settings.rules.filter((r) => r.paused).map((r) => r.key));

  const periodLabel = (rule: RetentionRuleSetting) =>
    rule.period
      ? t(rule.period.unit === "months" ? "periodMonths" : "periodDays", { count: rule.period.value })
      : t("periodDays", { count: rule.days });

  return (
    <div className="space-y-10" data-retention-panel>
      {!canManage && <p className="text-sm text-muted-foreground">{t("readOnly")}</p>}
      <ActionError code={error} returnPath={`/${locale}/settings/retention`} totpEnabled={totpEnabled} />

      <section aria-labelledby={`${ids}-rules`} className="space-y-3">
        <h2 id={`${ids}-rules`} className="font-display text-xl">
          {t("rulesTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("rulesIntro", { days: settings.preview_days })}</p>
        <ul className="divide-y card-hairline">
          {settings.rules.map((rule) => {
            const editable = canManage && (rule.period !== null || rule.configurable_days);
            const value = periods[rule.key] ?? "";
            return (
              <li key={rule.key} className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-start sm:justify-between" data-rule={rule.key}>
                <div className="min-w-0 space-y-1">
                  <p className="font-medium">{ruleName(rule.key)}</p>
                  <p className="text-sm text-muted-foreground">{t(`mode.${rule.mode}` as "mode.rows")}</p>
                  <p className="text-xs text-muted-foreground">{t("keeps", { period: periodLabel(rule) })}</p>
                  {rule.paused && rule.paused_at && (
                    <p className="text-sm" data-paused-note>
                      {t("pausedNote", { date: date(rule.paused_at), days: settings.preview_days })}
                    </p>
                  )}
                  {editable && (
                    <form
                      className="flex flex-wrap items-center gap-2 pt-1"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const n = Number(value);
                        if (!Number.isInteger(n)) return setError("invalid_input");
                        void saveRule(rule, rule.period ? { period: n } : { days: n });
                      }}
                    >
                      <label htmlFor={`${ids}-${rule.key}`} className="text-xs text-muted-foreground">
                        {rule.period?.unit === "months" ? t("months") : t("days")}
                      </label>
                      <Input
                        id={`${ids}-${rule.key}`}
                        type="number"
                        inputMode="numeric"
                        className="h-8 w-24"
                        min={rule.period ? rule.period.minimum : 0}
                        max={rule.period ? rule.period.maximum : 36500}
                        value={value}
                        onChange={(e) => setPeriods((all) => ({ ...all, [rule.key]: e.target.value }))}
                      />
                      <Button type="submit" size="sm" variant="outline" disabled={busy !== null}>
                        {t("savePeriod")}
                      </Button>
                    </form>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  {busy === rule.key && <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden />}
                  {rule.paused && <Badge variant="outline">{t("paused")}</Badge>}
                  {canManage && rule.enabled && rule.mode !== "report" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy !== null}
                      onClick={() => void togglePause(rule)}
                      aria-label={t(rule.paused ? "resumeLabel" : "pauseLabel", { rule: ruleName(rule.key) })}
                    >
                      {rule.paused ? <Play aria-hidden /> : <Pause aria-hidden />}
                      {rule.paused ? t("resume") : t("pause")}
                    </Button>
                  )}
                  <span className="text-xs text-muted-foreground">{rule.enabled ? t("on") : t("off")}</span>
                  <Switch
                    checked={rule.enabled}
                    disabled={!canManage || busy !== null || rule.mode === "report"}
                    onCheckedChange={(enabled) => void saveRule(rule, { enabled })}
                    aria-label={t("toggle", { rule: ruleName(rule.key) })}
                  />
                </div>
              </li>
            );
          })}
        </ul>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            const n = Number(cap);
            if (!Number.isInteger(n) || n < 1) return setError("invalid_input");
            void run<{ settings: RetentionSettings }>("cap", () => setCap(n), ({ settings: next }) => setSettings(next));
          }}
        >
          <div className="space-y-1">
            <label htmlFor={`${ids}-cap`} className="block text-sm font-medium">
              {t("capLabel")}
            </label>
            <p className="text-xs text-muted-foreground">{t("capHint")}</p>
            <Input
              id={`${ids}-cap`}
              type="number"
              inputMode="numeric"
              className="w-32"
              min={1}
              max={10000}
              value={cap}
              disabled={!canManage}
              onChange={(e) => setCapValue(e.target.value)}
            />
          </div>
          {canManage && (
            <Button type="submit" variant="outline" disabled={busy !== null}>
              {t("saveCap")}
            </Button>
          )}
        </form>
      </section>

      <section aria-labelledby={`${ids}-preview`} className="space-y-3" data-retention-preview>
        <h2 id={`${ids}-preview`} className="font-display text-xl">
          {t("previewTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("previewIntro", { days: settings.preview_days })}</p>
        {schedule.length === 0 ? (
          <EmptyState title={t("previewEmpty")} />
        ) : (
          <ul className="divide-y card-hairline">
            {schedule.map((item) => {
              const isHeld = item.status === "held" || heldIds.has(`${item.subject_type}:${item.subject_id}`);
              return (
                <li key={item.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between" data-schedule-item={item.id} data-status={item.status}>
                  <div className="min-w-0 space-y-0.5">
                    <p className="text-sm font-medium">
                      {t(`subject.${item.subject_type === "hire.candidate" ? "candidate" : "other"}` as "subject.candidate")}{" "}
                      <span dir="ltr" className="font-mono">
                        {item.label ?? item.subject_id.slice(0, 8)}
                      </span>
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {ruleName(item.rule)} · {t("dueSince", { date: date(item.due_at) })}
                    </p>
                    <p className="text-sm">
                      {item.status === "held"
                        ? t("heldNote")
                        : item.status === "erasing"
                          ? t("erasingNote")
                          : pausedRules.has(item.rule)
                            ? t("pausedItem")
                            : t("scheduledFor", { date: date(item.delete_after) })}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{t(`status.${item.status}` as "status.scheduled")}</Badge>
                    {canManage && !isHeld && item.status === "scheduled" && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        onClick={() => {
                          setHoldFor(item);
                          setReason("");
                        }}
                      >
                        <Lock aria-hidden />
                        {t("hold")}
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {holdFor && (
          <form
            className="space-y-2 card-hairline p-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!reason.trim()) return;
              const target = holdFor;
              void run<{ hold: RetentionHold }>(
                "hold",
                () => hold({ subjectType: target.subject_type, subjectId: target.subject_id, reason: reason.trim() }),
                ({ hold: placed }) => {
                  setHolds((all) => [placed, ...all]);
                  setSchedule((all) =>
                    all.map((i) => (i.subject_type === target.subject_type && i.subject_id === target.subject_id ? { ...i, status: "held" } : i)),
                  );
                  setHoldFor(null);
                },
              );
            }}
          >
            <label htmlFor={`${ids}-reason`} className="block text-sm font-medium">
              {t("holdReason", { subject: holdFor.label ?? holdFor.subject_id.slice(0, 8) })}
            </label>
            <Textarea id={`${ids}-reason`} rows={3} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={busy !== null || !reason.trim()}>
                {t("placeHold")}
              </Button>
              <Button type="button" variant="ghost" onClick={() => setHoldFor(null)}>
                {t("cancel")}
              </Button>
            </div>
          </form>
        )}
      </section>

      <section aria-labelledby={`${ids}-holds`} className="space-y-3">
        <h2 id={`${ids}-holds`} className="font-display text-xl">
          {t("holdsTitle")}
        </h2>
        {holds.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("holdsEmpty")}</p>
        ) : (
          <ul className="divide-y card-hairline">
            {holds.map((h) => (
              <li key={h.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between" data-hold={h.id}>
                <div className="min-w-0 space-y-0.5">
                  <p className="text-sm font-medium">
                    {t(`subject.${h.subject_type === "hire.candidate" ? "candidate" : h.subject_type === "pay.vendor" ? "vendor" : "other"}` as "subject.candidate")}{" "}
                    <span dir="ltr" className="font-mono">
                      {h.subject_id.slice(0, 8)}
                    </span>
                  </p>
                  <p className="text-xs text-muted-foreground">{t("heldSince", { date: date(h.placed_at) })}</p>
                  {h.reason && <p className="text-sm whitespace-pre-wrap">{h.reason}</p>}
                </div>
                {canManage && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy !== null}
                    onClick={() =>
                      void run<{ hold: RetentionHold }>("release", () => release(h.id), () => {
                        setHolds((all) => all.filter((x) => x.id !== h.id));
                        setSchedule((all) =>
                          all.map((i) => (i.subject_type === h.subject_type && i.subject_id === h.subject_id && i.status === "held" ? { ...i, status: "scheduled" } : i)),
                        );
                      })
                    }
                  >
                    <LockOpen aria-hidden />
                    {t("release")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

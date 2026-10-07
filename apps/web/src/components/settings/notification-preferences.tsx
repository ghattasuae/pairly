// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { BellOff, Moon } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { FormError } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { Checkbox, Select, Switch } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import { type PreferencesForm, savePreferences } from "@/lib/actions/notifications";
import { errorKey } from "@/lib/action-result";
import type { Preferences } from "@/lib/api/types";
import { minutesToTime, SNOOZE_OPTIONS, snoozeEnd, timeToMinutes } from "@/lib/notification-prefs";

const CHANNELS = ["email", "slack", "in_app"] as const;
type Channel = (typeof CHANNELS)[number];

function timeZones(current: string): string[] {
  let zones: string[];
  try {
    zones = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  } catch {
    zones = [];
  }
  if (!zones.includes("UTC")) zones = ["UTC", ...zones];
  return zones.includes(current) ? zones : [current, ...zones];
}

/**
 * Notification preferences (brief 5.3, Notifications UX): channels per category, real
 * time or a daily digest, snooze, quiet hours. Urgent items (SLA breaches, approvals
 * blocking others) still arrive in real time; the page says so.
 */
export function NotificationPreferences({
  initial,
  save = savePreferences,
}: {
  initial: Preferences;
  /** Override for stories and tests. Defaults to the server action. */
  save?: typeof savePreferences;
}) {
  const t = useTranslations("NotificationSettings");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const toast = useToast();
  const ids = { tz: useId(), locale: useId(), snooze: useId(), qs: useId(), qe: useId(), error: useId() };
  const [prefs, setPrefs] = useState<Preferences>(initial);
  const [quietOn, setQuietOn] = useState(initial.quiet_start !== null);
  const [quietStart, setQuietStart] = useState(minutesToTime(initial.quiet_start ?? 18 * 60));
  const [quietEnd, setQuietEnd] = useState(minutesToTime(initial.quiet_end ?? 8 * 60));
  const [snooze, setSnooze] = useState<string>("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const zones = useMemo(() => timeZones(prefs.timezone), [prefs.timezone]);
  const categories = useTranslations("NotificationSettings.categories");

  const toggle = (category: string, channel: Channel, on: boolean) =>
    setPrefs((p) => {
      const current = new Set(p.channels[category] ?? []);
      if (on) current.add(channel);
      else current.delete(channel);
      return { ...p, channels: { ...p.channels, [category]: CHANNELS.filter((c) => current.has(c)) } };
    });

  const snoozedUntil = prefs.snooze_until && new Date(prefs.snooze_until) > new Date() ? new Date(prefs.snooze_until) : null;

  const submit = async (override?: Partial<PreferencesForm>) => {
    const start = quietOn ? timeToMinutes(quietStart) : null;
    const end = quietOn ? timeToMinutes(quietEnd) : null;
    if (quietOn && (start === null || end === null || start === end)) {
      setError("quiet_hours");
      return;
    }
    const snoozeUntil = snooze
      ? snoozeEnd(snooze as (typeof SNOOZE_OPTIONS)[number]).toISOString()
      : snoozedUntil
        ? snoozedUntil.toISOString()
        : null;
    const body: PreferencesForm = {
      locale: prefs.locale === "ar" ? "ar" : "en",
      timezone: prefs.timezone,
      channels: Object.fromEntries(prefs.categories.map((c) => [c, prefs.channels[c] ?? []])) as PreferencesForm["channels"],
      digest: Object.fromEntries(prefs.categories.map((c) => [c, prefs.digest[c] === "daily" ? "daily" : "realtime"])) as PreferencesForm["digest"],
      snooze_until: snoozeUntil,
      quiet_start: start,
      quiet_end: end,
      ...override,
    };
    setPending(true);
    setError(null);
    try {
      const result = await save(body);
      if (result.ok) {
        setPrefs(result.preferences);
        setSnooze("");
        toast.show({ tone: "success", title: t("saved") });
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  return (
    <form
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      className="space-y-8"
    >
      <section aria-labelledby="channels-heading" className="space-y-3">
        <div className="space-y-1">
          <h2 id="channels-heading" className="font-display text-xl">
            {t("channelsTitle")}
          </h2>
          <p className="text-sm text-muted-foreground">{t("channelsBody")}</p>
        </div>
        <div className="overflow-x-auto rounded-xl border bg-surface">
          <table className="w-full min-w-[36rem] text-sm">
            <caption className="sr-only">{t("channelsTitle")}</caption>
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("category")}</th>
                {CHANNELS.map((c) => (
                  <th key={c} scope="col" className="px-3 py-2.5 text-center font-medium">
                    {t(`channels.${c}`)}
                  </th>
                ))}
                <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("delivery")}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {prefs.categories.map((category) => {
                const name = categories.has(category as "approvals") ? categories(category as "approvals") : category;
                return (
                  <tr key={category}>
                    <th scope="row" className="px-4 py-3 text-start font-normal">
                      <span className="block font-medium">{name}</span>
                      {category === "sla" && <span className="block text-xs text-muted-foreground">{t("alwaysUrgent")}</span>}
                    </th>
                    {CHANNELS.map((channel) => (
                      <td key={channel} className="px-3 py-3 text-center">
                        <Checkbox
                          checked={(prefs.channels[category] ?? []).includes(channel)}
                          onChange={(e) => toggle(category, channel, e.target.checked)}
                          aria-label={t("channelFor", { channel: t(`channels.${channel}`), category: name })}
                        />
                      </td>
                    ))}
                    <td className="px-4 py-3">
                      <Select
                        aria-label={t("deliveryFor", { category: name })}
                        value={prefs.digest[category] === "daily" ? "daily" : "realtime"}
                        onChange={(e) =>
                          setPrefs((p) => ({ ...p, digest: { ...p.digest, [category]: e.target.value } }))
                        }
                        className="h-9 w-40"
                      >
                        <option value="realtime">{t("realtime")}</option>
                        <option value="daily">{t("daily")}</option>
                      </Select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-muted-foreground">{t("urgentNote")}</p>
      </section>

      <section aria-labelledby="pause-heading" className="grid gap-6 md:grid-cols-2">
        <div className="space-y-3 rounded-xl border bg-surface p-5">
          <h2 id="pause-heading" className="flex items-center gap-2 font-display text-xl">
            <BellOff className="size-5 text-primary" aria-hidden />
            {t("snoozeTitle")}
          </h2>
          <p className="text-sm text-muted-foreground">
            {snoozedUntil
              ? t("snoozedUntil", { when: format.dateTime(snoozedUntil, { dateStyle: "medium", timeStyle: "short" }) })
              : t("notSnoozed")}
          </p>
          <label htmlFor={ids.snooze} className="text-sm font-medium">
            {t("snoozeFor")}
          </label>
          <Select id={ids.snooze} value={snooze} onChange={(e) => setSnooze(e.target.value)}>
            <option value="">{snoozedUntil ? t("keepSnooze") : t("noSnooze")}</option>
            {SNOOZE_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {t(`snooze.${o}`)}
              </option>
            ))}
          </Select>
          {snoozedUntil && (
            <Button type="button" variant="outline" size="sm" disabled={pending} onClick={() => void submit({ snooze_until: null })}>
              {t("endSnooze")}
            </Button>
          )}
        </div>

        <div className="space-y-3 rounded-xl border bg-surface p-5">
          <div className="flex items-center justify-between gap-3">
            <h2 className="flex items-center gap-2 font-display text-xl">
              <Moon className="size-5 text-primary" aria-hidden />
              {t("quietTitle")}
            </h2>
            <Switch checked={quietOn} onCheckedChange={setQuietOn} aria-label={t("quietToggle")} />
          </div>
          <p className="text-sm text-muted-foreground">{t("quietBody")}</p>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label htmlFor={ids.qs} className="text-sm font-medium">
                {t("quietStart")}
              </label>
              <Input id={ids.qs} type="time" dir="ltr" value={quietStart} disabled={!quietOn} onChange={(e) => setQuietStart(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={ids.qe} className="text-sm font-medium">
                {t("quietEnd")}
              </label>
              <Input id={ids.qe} type="time" dir="ltr" value={quietEnd} disabled={!quietOn} onChange={(e) => setQuietEnd(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={ids.tz} className="text-sm font-medium">
              {t("timezone")}
            </label>
            <Select id={ids.tz} value={prefs.timezone} onChange={(e) => setPrefs((p) => ({ ...p, timezone: e.target.value }))}>
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </section>

      <section className="max-w-sm space-y-1.5">
        <label htmlFor={ids.locale} className="text-sm font-medium">
          {t("language")}
        </label>
        <Select id={ids.locale} value={prefs.locale} onChange={(e) => setPrefs((p) => ({ ...p, locale: e.target.value }))}>
          <option value="en">English</option>
          <option value="ar">العربية</option>
        </Select>
      </section>

      {error && (
        <FormError id={ids.error}>{error === "quiet_hours" ? t("quietInvalid") : tErrors(errorKey(error) as "generic")}</FormError>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? t("saving") : t("save")}
      </Button>
    </form>
  );
}

// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { CalendarCheck2, CalendarOff, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useState } from "react";

import { TimeZoneSelect } from "@/components/hire/scheduling/scheduling-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox, FieldRow, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import * as calendarActions from "@/lib/actions/scheduling";
import type { Availability, CalendarIntegrations } from "@/lib/hire/scheduling";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
type Day = (typeof DAYS)[number];

type ConnectKey = calendarActions.CalendarProviderKey;
const CONNECTABLE = new Set<string>(["google", "microsoft"]);
// Which provider a consent round trip started with (the state token does not say).
const PENDING_KEY = "pairly.calendar.connect";

function rememberPending(provider: ConnectKey) {
  try {
    window.sessionStorage.setItem(PENDING_KEY, provider);
  } catch {
    // Storage off: the page falls back to the hint in the callback's query.
  }
}

function takePending(): ConnectKey | null {
  try {
    const value = window.sessionStorage.getItem(PENDING_KEY);
    window.sessionStorage.removeItem(PENDING_KEY);
    return value === "google" || value === "microsoft" ? value : null;
  } catch {
    return null;
  }
}

/**
 * Settings, Calendar: the person's calendar connections and when they can interview.
 *
 * Google Calendar and Microsoft 365 each show their state and, when the organisation and
 * this server have them set up, a connect or disconnect button for this person. A
 * provider that is not set up shows the steps an administrator takes; nothing here
 * creates an account. Zoom is the organisation's (see `CalendarOrgSettings`). After the
 * consent screen the browser comes back here with `?code&state`, finished once.
 */
export function CalendarSettings({
  initialIntegrations,
  initialAvailability,
  callback,
  actions = calendarActions,
}: {
  initialIntegrations: CalendarIntegrations;
  initialAvailability: Availability | null;
  callback?: { code: string; state: string; hint?: ConnectKey } | null;
  actions?: Pick<typeof calendarActions, "startConnect" | "finishConnect" | "disconnectCalendar" | "saveMyAvailability">;
}) {
  const t = useTranslations("CalendarSettings");
  const [integrations, setIntegrations] = useState(initialIntegrations);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!callback) return;
    window.history.replaceState(null, "", window.location.pathname);
    const provider = takePending() ?? callback.hint ?? "google";
    let cancelled = false;
    void actions.finishConnect(provider, callback.code, callback.state).then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setIntegrations(result.integrations);
        setMessage(t("connectedNow", { provider: t(`name.${provider}`) }));
      } else setMessage(t("invalid"));
    });
    return () => {
      cancelled = true;
    };
  }, [actions, callback, t]);

  const connect = async (provider: ConnectKey) => {
    setBusy(provider);
    const result = await actions.startConnect(provider);
    setBusy(null);
    if (result.ok) {
      rememberPending(provider);
      window.location.assign(result.url);
    } else setMessage(t("invalid"));
  };

  const disconnect = async (provider: ConnectKey) => {
    setBusy(provider);
    const result = await actions.disconnectCalendar(provider);
    setBusy(null);
    if (result.ok) setIntegrations(result.integrations);
    else setMessage(t("invalid"));
  };

  // The steps in the page's language (the API's are English); the API's as a fallback.
  const setupSteps = (provider: string, fallback: string[]): string[] => {
    if (provider !== "google" && provider !== "microsoft" && provider !== "zoom") return fallback;
    const found: unknown = t.raw(`setup.${provider}`);
    return Array.isArray(found) && found.every((s) => typeof s === "string") ? found : fallback;
  };

  const using =
    integrations.using === "google" || integrations.using === "microsoft"
      ? t(`use.${integrations.using}`)
      : t("use.fake");

  return (
    <div className="space-y-10">
      <section aria-labelledby="calendar-connections" className="space-y-4">
        <h2 id="calendar-connections" className="font-display text-2xl">
          {t("connections")}
        </h2>
        <p className="text-sm" data-calendar-using={integrations.using}>
          {t("using", { provider: using })}
        </p>
        {message && (
          <p role="status" className="text-sm font-medium">
            {message}
          </p>
        )}
        <ul className="grid gap-3 md:grid-cols-3">
          {integrations.providers.map((provider) => {
            const key = CONNECTABLE.has(provider.provider) ? (provider.provider as ConnectKey) : null;
            const label = t(`name.${provider.provider === "zoom" ? "zoom" : (key ?? "google")}`);
            return (
              <li
                key={provider.provider}
                className="space-y-3 rounded-xl border bg-surface p-5 shadow-soft"
                data-calendar-provider={provider.provider}
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="font-medium">{label}</p>
                  <Badge variant={provider.connected ? "default" : "outline"}>
                    {provider.connected ? (
                      <CalendarCheck2 className="size-3" aria-hidden />
                    ) : (
                      <CalendarOff className="size-3" aria-hidden />
                    )}
                    {provider.connected
                      ? t("connectedShort")
                      : t(provider.availability === "available" ? "status.available" : "status.needs_setup")}
                  </Badge>
                </div>
                {provider.connected && provider.account_email && (
                  <p className="text-xs text-muted-foreground">{t("connected", { email: provider.account_email })}</p>
                )}
                {provider.provider === "zoom" && <p className="text-xs text-muted-foreground">{t("zoomPersonal")}</p>}
                {!provider.connected && provider.availability !== "available" && (
                  <details className="text-sm">
                    <summary className="cursor-pointer font-medium">{t("steps")}</summary>
                    <ol className="mt-2 list-decimal space-y-1 ps-5 text-xs text-muted-foreground" dir="auto">
                      {setupSteps(provider.provider, provider.steps).map((step) => (
                        <li key={step}>{step}</li>
                      ))}
                    </ol>
                  </details>
                )}
                {key &&
                  (provider.connected ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void disconnect(key)}
                      disabled={busy !== null}
                      aria-label={t("disconnectNamed", { provider: label })}
                    >
                      {t("disconnect")}
                    </Button>
                  ) : (
                    provider.availability === "available" && (
                      <Button type="button" size="sm" onClick={() => void connect(key)} disabled={busy !== null}>
                        {busy === key && <Loader2 className="animate-spin" aria-hidden />}
                        {t(`connect.${key}`)}
                      </Button>
                    )
                  ))}
              </li>
            );
          })}
        </ul>
      </section>
      {initialAvailability && <AvailabilityForm initial={initialAvailability} save={actions.saveMyAvailability} />}
    </div>
  );
}

function AvailabilityForm({
  initial,
  save,
}: {
  initial: Availability;
  save: typeof calendarActions.saveMyAvailability;
}) {
  const t = useTranslations("CalendarSettings");
  const ids = useId();
  const [zone, setZone] = useState(initial.timezone);
  const [hours, setHours] = useState<Record<Day, { on: boolean; from: string; to: string }>>(() =>
    Object.fromEntries(
      DAYS.map((d) => {
        const first = initial.working_hours[d]?.[0];
        return [d, { on: Boolean(first), from: first?.[0] ?? "09:00", to: first?.[1] ?? "17:00" }];
      }),
    ) as Record<Day, { on: boolean; from: string; to: string }>,
  );
  const [before, setBefore] = useState(initial.buffer_before_min);
  const [after, setAfter] = useState(initial.buffer_after_min);
  const [maxPerDay, setMaxPerDay] = useState<string>(initial.max_per_day === null ? "" : String(initial.max_per_day));
  const [holidays, setHolidays] = useState(initial.holidays.join("\n"));
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <section aria-labelledby={`${ids}-title`} className="space-y-4">
      <div className="space-y-1">
        <h2 id={`${ids}-title`} className="font-display text-2xl">
          {t("availability")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("availabilityHelp")}</p>
      </div>
      <form
        className="space-y-4 rounded-xl border bg-surface p-5 shadow-soft"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          const workingHours: Record<string, [string, string][]> = {};
          for (const d of DAYS) if (hours[d].on) workingHours[d] = [[hours[d].from, hours[d].to]];
          const result = await save({
            timezone: zone,
            workingHours,
            bufferBeforeMin: before,
            bufferAfterMin: after,
            maxPerDay: maxPerDay.trim() === "" ? null : Number(maxPerDay),
            holidays: holidays
              .split(/\s+/)
              .map((h) => h.trim())
              .filter(Boolean),
          });
          setBusy(false);
          setStatus(result.ok ? t("saved") : t("invalid"));
        }}
      >
        <FieldRow label={t("timezone")} htmlFor={`${ids}-tz`}>
          <TimeZoneSelect id={`${ids}-tz`} value={zone} onChange={setZone} className="max-w-xs" />
        </FieldRow>
        <table className="w-full max-w-lg text-sm">
          <thead>
            <tr className="text-start text-xs text-muted-foreground">
              <th className="py-1 text-start font-normal" scope="col">
                <span className="sr-only">{t("days.mon")}</span>
              </th>
              <th className="py-1 text-start font-normal" scope="col">
                {t("from")}
              </th>
              <th className="py-1 text-start font-normal" scope="col">
                {t("to")}
              </th>
            </tr>
          </thead>
          <tbody>
            {DAYS.map((d) => (
              <tr key={d}>
                <th scope="row" className="py-1 pe-3 text-start font-normal">
                  <label className="flex items-center gap-2">
                    <Checkbox
                      checked={hours[d].on}
                      aria-label={t("works", { day: t(`days.${d}`) })}
                      onChange={(e) => setHours((h) => ({ ...h, [d]: { ...h[d], on: e.target.checked } }))}
                    />
                    {t(`days.${d}`)}
                  </label>
                </th>
                <td className="py-1 pe-2">
                  <Input
                    type="time"
                    aria-label={`${t(`days.${d}`)} ${t("from")}`}
                    value={hours[d].from}
                    disabled={!hours[d].on}
                    onChange={(e) => setHours((h) => ({ ...h, [d]: { ...h[d], from: e.target.value } }))}
                    className="h-8"
                    dir="ltr"
                  />
                </td>
                <td className="py-1">
                  <Input
                    type="time"
                    aria-label={`${t(`days.${d}`)} ${t("to")}`}
                    value={hours[d].to}
                    disabled={!hours[d].on}
                    onChange={(e) => setHours((h) => ({ ...h, [d]: { ...h[d], to: e.target.value } }))}
                    className="h-8"
                    dir="ltr"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="grid max-w-lg gap-4 sm:grid-cols-3">
          <FieldRow label={t("before")} htmlFor={`${ids}-before`}>
            <Input id={`${ids}-before`} type="number" min={0} max={240} value={before} onChange={(e) => setBefore(Number(e.target.value))} />
          </FieldRow>
          <FieldRow label={t("after")} htmlFor={`${ids}-after`}>
            <Input id={`${ids}-after`} type="number" min={0} max={240} value={after} onChange={(e) => setAfter(Number(e.target.value))} />
          </FieldRow>
          <FieldRow label={t("maxPerDay")} htmlFor={`${ids}-max`}>
            <Input id={`${ids}-max`} type="number" min={0} max={24} value={maxPerDay} onChange={(e) => setMaxPerDay(e.target.value)} />
          </FieldRow>
        </div>
        <FieldRow label={t("holidays")} htmlFor={`${ids}-holidays`}>
          <Textarea id={`${ids}-holidays`} rows={3} dir="ltr" value={holidays} onChange={(e) => setHolidays(e.target.value)} className="max-w-xs" />
        </FieldRow>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={busy}>
            {t("save")}
          </Button>
          {status && (
            <p role="status" className="text-sm">
              {status}
            </p>
          )}
        </div>
      </form>
    </section>
  );
}

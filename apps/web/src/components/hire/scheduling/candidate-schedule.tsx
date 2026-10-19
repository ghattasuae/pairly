// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { CalendarCheck2, CalendarX2, Loader2, Video } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import {
  bookSchedule,
  cancelSchedule,
  readSchedule,
  type ScheduleView,
} from "@/lib/actions/candidate-schedule";
import { browserTimeZone, clock, dayLabel, isTimeZone, localDate, whenLabel, type PublicSlot } from "@/lib/hire/scheduling";
import { cn } from "@/lib/utils";

import { TimeZoneSelect } from "./scheduling-panel";

const STORE_KEY = "pairly.schedule.token";

function takeToken(): string {
  const raw = window.location.hash.replace(/^#/, "");
  if (raw) {
    window.history.replaceState(null, "", window.location.pathname);
    try {
      window.sessionStorage.setItem(STORE_KEY, raw);
    } catch {
      // Private mode: the token lives in memory only.
    }
    return raw;
  }
  try {
    return window.sessionStorage.getItem(STORE_KEY) ?? "";
  } catch {
    return "";
  }
}

function keepToken(token: string) {
  try {
    window.sessionStorage.setItem(STORE_KEY, token);
  } catch {
    // Ignored, see takeToken.
  }
}

/**
 * `/<locale>/schedule#<token>`: a candidate chooses, changes or cancels their interview
 * time. The token stays in the fragment (never sent to a server by the browser, kept in
 * this tab's session storage so a language switch keeps it) and is posted in a JSON body.
 * Times show in the candidate's own time zone (the browser's, which they can change).
 */
export function CandidateSchedule({
  read = readSchedule,
  book = bookSchedule,
  cancel = cancelSchedule,
}: {
  read?: typeof readSchedule;
  book?: typeof bookSchedule;
  cancel?: typeof cancelSchedule;
}) {
  const t = useTranslations("SchedulePage");
  const locale = useLocale();
  const ids = useId();
  const [token, setToken] = useState("");
  const [zone, setZone] = useState("UTC");
  const [view, setView] = useState<ScheduleView | null>(null);
  const [picked, setPicked] = useState<PublicSlot | null>(null);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [changing, setChanging] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);

  useEffect(() => {
    const raw = takeToken();
    const tz = browserTimeZone();
    let cancelled = false;
    (raw ? read(raw, tz) : Promise.resolve<ScheduleView>({ kind: "invalid" }))
      .then((v) => {
        if (cancelled) return;
        setToken(raw);
        setZone(isTimeZone(tz) ? tz : "UTC");
        setView(v);
      })
      .catch(() => !cancelled && setView({ kind: "unavailable" }));
    return () => {
      cancelled = true;
    };
  }, [read]);

  if (view === null) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground" role="status">
        <Loader2 className="size-4 animate-spin" aria-hidden />
      </div>
    );
  }
  if (view.kind !== "ready") {
    const kind = view.kind === "taken" ? "invalid" : view.kind;
    return (
      <AuthCard title={t(`${kind}Title` as "invalidTitle")} subtitle={t(kind as "invalid")}>
        {null}
      </AuthCard>
    );
  }

  const schedule = view.schedule;
  const reload = async (tz: string, nextToken = token) => {
    const next = await read(nextToken, tz);
    setView(next.kind === "ready" ? next : view);
  };

  const changeZone = async (tz: string) => {
    setZone(tz);
    setPicked(null);
    await reload(tz);
  };

  const confirm = async () => {
    if (!picked) return;
    setWorking(true);
    setNotice(null);
    const next = await book(token, picked.start, zone);
    setWorking(false);
    if (next.kind === "taken") {
      setNotice(t("taken"));
      setPicked(null);
      await reload(zone);
      return;
    }
    if (next.kind === "ready" && next.schedule.token) {
      setToken(next.schedule.token);
      keepToken(next.schedule.token);
    }
    setPicked(null);
    setChanging(false);
    setView(next);
  };

  const doCancel = async () => {
    setWorking(true);
    const next = await cancel(token, zone);
    setWorking(false);
    setConfirmCancel(false);
    setView(next);
  };

  const subtitle = t("subtitle", { organisation: schedule.organisation, job: schedule.job_title });

  if (schedule.state === "cancelled" || schedule.state === "closed") {
    return (
      <AuthCard
        title={schedule.state === "cancelled" ? t("cancelledTitle") : t("closedTitle")}
        subtitle={subtitle}
      >
        <p className="flex items-start gap-2 text-sm" role="status">
          <CalendarX2 className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
          {schedule.state === "cancelled" ? t("cancelledBody") : t("closed")}
        </p>
      </AuthCard>
    );
  }

  const booked = schedule.state === "booked" && schedule.starts_at;
  const days = new Map<string, PublicSlot[]>();
  for (const slot of schedule.slots) {
    const list = days.get(slot.date) ?? [];
    list.push(slot);
    days.set(slot.date, list);
  }

  return (
    <div className="rise w-full max-w-md space-y-6" data-schedule-page>
      <div className="space-y-2">
        <h1 className="font-display text-[2rem] leading-[1.1] font-normal text-balance sm:text-[2.6rem]">
          {booked && !changing ? t("bookedTitle") : t("title")}
        </h1>
        <p className="text-sm text-muted-foreground">{subtitle}</p>
        <p className="text-sm">
          {t("details", { minutes: schedule.duration_min, count: schedule.panel_size })}
        </p>
      </div>

      {booked && !changing ? (
        <div className="space-y-4" data-booked>
          <p className="flex items-start gap-2 card-hairline px-4 py-3 text-sm" role="status">
            <CalendarCheck2 className="mt-0.5 size-4 shrink-0 text-success-ink" aria-hidden />
            <span>
              {t("bookedBody", {
                when: whenLabel(schedule.starts_at ?? "", zone, locale),
                zone,
              })}
            </span>
          </p>
          {schedule.meeting_url && (
            <a
              href={schedule.meeting_url}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-2 text-sm font-medium text-primary underline"
            >
              <Video className="size-4" aria-hidden />
              {t("join")}
            </a>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setChanging(true)} disabled={working}>
              {t("change")}
            </Button>
            {confirmCancel ? (
              <>
                <Button type="button" variant="destructive" size="sm" onClick={() => void doCancel()} disabled={working}>
                  {t("cancelConfirm")}
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmCancel(false)}>
                  {t("keep")}
                </Button>
              </>
            ) : (
              <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmCancel(true)} disabled={working}>
                {t("cancel")}
              </Button>
            )}
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor={`${ids}-zone`} className="text-sm font-medium">
              {t("zone")}
            </label>
            <TimeZoneSelect id={`${ids}-zone`} value={zone} onChange={(z) => void changeZone(z)} />
            <p className="text-xs text-muted-foreground">{t("zoneHelp")}</p>
          </div>
          {notice && (
            <p role="alert" className="text-sm text-danger-ink">
              {notice}
            </p>
          )}
          <section aria-labelledby={`${ids}-pick`} className="space-y-3">
            <h2 id={`${ids}-pick`} className="text-sm font-medium">
              {t("pick")}
            </h2>
            {schedule.slots.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("noSlots")}</p>
            ) : (
              [...days.entries()].map(([date, slots]) => (
                <div key={date} className="space-y-1.5">
                  <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase" dir="auto">
                    {dayLabel(date, locale)}
                  </h3>
                  <ul className="flex flex-wrap gap-1.5">
                    {slots.map((slot) => {
                      const on = picked?.start === slot.start;
                      return (
                        <li key={slot.start}>
                          <button
                            type="button"
                            aria-pressed={on}
                            data-candidate-slot={slot.start}
                            onClick={() => setPicked(slot)}
                            className={cn(
                              "rounded-md border px-3 py-1.5 text-sm tabular-nums",
                              on ? "border-primary bg-primary text-primary-foreground" : "border-input bg-surface hover:bg-accent",
                            )}
                            title={slot.sociable ? undefined : t("early")}
                          >
                            <span dir="ltr">{slot.time}</span>
                            {!slot.sociable && <span className="sr-only">, {t("early")}</span>}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ))
            )}
          </section>
          {picked && (
            <div className="space-y-2 card-hairline px-4 py-3">
              <p className="text-sm" role="status">
                {t("selected", {
                  when: `${dayLabel(localDate(picked.start, zone), locale)}, ${clock(picked.start, zone)}`,
                })}
              </p>
              <Button type="button" onClick={() => void confirm()} disabled={working} data-confirm-slot>
                {working && <Loader2 className="animate-spin" aria-hidden />}
                {working ? t("booking") : t("confirm")}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

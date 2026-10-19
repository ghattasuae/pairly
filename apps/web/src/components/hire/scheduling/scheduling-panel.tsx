// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import {
  CalendarClock,
  CalendarX2,
  ChevronLeft,
  ChevronRight,
  EyeOff,
  Link2,
  Loader2,
  Pencil,
  Send,
  TriangleAlert,
  UserCheck,
  UserX,
  Video,
  X,
} from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Checkbox, FieldRow, Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import * as schedulingActions from "@/lib/actions/scheduling";
import {
  ACTIVE_STATUSES,
  OPEN_STATUSES,
  addDays,
  clock,
  dayLabel,
  instantAt,
  localDate,
  minutesOfDay,
  nextDates,
  overlaps,
  whenLabel,
  type Interview,
  type SchedulingPanelData,
  type SlotCheck,
  type SlotsResult,
} from "@/lib/hire/scheduling";
import { cn } from "@/lib/utils";

export type SchedulingActions = typeof schedulingActions;

/**
 * The scheduling panel (brief 3.6 and 5.3, Scheduling UX). Entry point for every place
 * that schedules a candidate: the candidate panel beside the board, and screening mode.
 *
 *   <SchedulingPanel applicationId={id} onClose={back} onChanged={refresh} />
 *
 * - Suggested times are already free for every panelist (their calendars, working hours,
 *   buffers, daily caps, holidays); one click books one, one click sends the candidate a
 *   self-scheduling link instead.
 * - A booked interview moves with one click on a suggested time, or by dragging it on
 *   the small calendar (arrow keys and Enter do the same). A time that is not free for
 *   everyone shows who and why, inline, with "Move anyway".
 * - Times show in the viewer's zone with the candidate's local time beside them.
 * - Editing (time, length, panel, meeting link, the candidate's zone) is one form.
 * - The API sends every email and calendar update; nothing here talks to a calendar.
 */
export function SchedulingPanel({
  applicationId,
  onClose,
  onChanged,
  actions: overrides,
}: {
  applicationId: string;
  onClose: () => void;
  onChanged?: ((interview: Interview) => void) | undefined;
  actions?: Partial<SchedulingActions> | undefined;
}) {
  const t = useTranslations("Scheduling");
  const locale = useLocale();
  const toast = useToast();
  const ids = useId();
  const actions = useMemo(() => ({ ...schedulingActions, ...overrides }), [overrides]);

  const [data, setData] = useState<SchedulingPanelData | null>(null);
  const [panel, setPanel] = useState<string[]>([]);
  const [duration, setDuration] = useState(45);
  const [candidateTz, setCandidateTz] = useState("UTC");
  const [slots, setSlots] = useState<SlotsResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ startsAt: string; check: SlotCheck } | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);

  const active = useMemo(
    () => data?.interviews.find((i) => OPEN_STATUSES.has(i.status)) ?? null,
    [data],
  );
  const viewerTz = data?.viewer_timezone ?? "UTC";

  const load = useCallback(async () => {
    const result = await actions.loadScheduling(applicationId);
    if (!result.ok) {
      setError(result.code);
      return null;
    }
    setData(result.data);
    return result.data;
  }, [actions, applicationId]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const loaded = await load();
      if (cancelled || !loaded) return;
      setPanel(loaded.default_panel);
      setDuration(loaded.duration_min);
      setCandidateTz(loaded.candidate_timezone);
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  const activeId = active?.id ?? null;
  const [slotsTick, setSlotsTick] = useState(0);
  const [slotsFor, setSlotsFor] = useState<string | null>(null);
  const slotsQuery = useMemo(
    () =>
      data && panel.length > 0
        ? { applicationId, panel, durationMin: duration, candidateTimezone: candidateTz, interviewId: activeId }
        : null,
    [data, applicationId, panel, duration, candidateTz, activeId],
  );
  const slotsKey = slotsQuery ? `${JSON.stringify(slotsQuery)}#${slotsTick}` : null;
  const loadingSlots = slotsKey !== null && slotsFor !== slotsKey;
  const refreshSlots = useCallback(() => setSlotsTick((n) => n + 1), []);

  useEffect(() => {
    if (!slotsQuery || !slotsKey) return;
    let cancelled = false;
    void actions.findSlots(slotsQuery).then((result) => {
      if (cancelled) return;
      if (result.ok) setSlots(result.slots);
      else setError(result.code);
      setSlotsFor(slotsKey);
    });
    return () => {
      cancelled = true;
    };
  }, [actions, slotsQuery, slotsKey]);

  const done = async (message: string, interview: Interview) => {
    toast.show({ tone: "success", title: message, duration: 4000 });
    setPending(null);
    setEditing(false);
    await load();
    onChanged?.(interview);
  };

  const fail = (code: string) => {
    setError(code);
    if (code === "slot_taken") refreshSlots();
  };

  const key = () => `sched-${crypto.randomUUID()}`;

  /** Book a new interview, book a proposed one, or move a booked one. */
  const choose = async (startsAt: string, force = false) => {
    setBusy(true);
    setError(null);
    if (active) {
      const result = await actions.updateInterview(active.id, { startsAt, panel, durationMin: duration, force });
      setBusy(false);
      if (!result.ok) return fail(result.code);
      return done(ACTIVE_STATUSES.has(active.status) ? t("moved") : t("booked"), result.interview);
    }
    const result = await actions.createInterview({
      applicationId,
      mode: "book",
      panel,
      durationMin: duration,
      startsAt,
      candidateTimezone: candidateTz,
      force,
      idempotencyKey: key(),
    });
    setBusy(false);
    if (!result.ok) return fail(result.code);
    return done(t("booked"), result.interview);
  };

  /** A time from the calendar: check it first, then move, or show who is not free. */
  const tryTime = async (startsAt: string) => {
    setError(null);
    const result = await actions.checkSlot({ applicationId, panel, startsAt, durationMin: duration, interviewId: activeId });
    if (!result.ok) return fail(result.code);
    if (result.check.ok) return choose(startsAt);
    setPending({ startsAt, check: result.check });
  };

  const sendLink = async () => {
    setBusy(true);
    setError(null);
    const result = active
      ? await actions.resendLink(active.id, key())
      : await actions.createInterview({
          applicationId,
          mode: "link",
          panel,
          durationMin: duration,
          candidateTimezone: candidateTz,
          idempotencyKey: key(),
        });
    setBusy(false);
    if (!result.ok) return fail(result.code);
    return done(t("linkSent"), result.interview);
  };

  const cancel = async () => {
    if (!active) return;
    setBusy(true);
    const result = await actions.cancelInterview(active.id, key());
    setBusy(false);
    setConfirmCancel(false);
    if (!result.ok) return fail(result.code);
    return done(t("cancelled"), result.interview);
  };

  const outcome = async (interview: Interview, value: "completed" | "no_show") => {
    setBusy(true);
    const result = await actions.recordOutcome(interview.id, value);
    setBusy(false);
    if (!result.ok) return fail(result.code);
    return done(t("outcomeSaved"), result.interview);
  };

  const errorText = error
    ? (["slot_taken", "calendar_unavailable", "conflict"] as const).includes(error as "conflict")
      ? t(`error.${error}` as "error.conflict")
      : t("error.generic")
    : null;

  if (!data) {
    return (
      <div className="flex h-full flex-col">
        <PanelHeader title={t("title")} onClose={onClose} closeLabel={t("close")} />
        <div className="flex flex-1 items-center justify-center text-muted-foreground" role="status">
          {error ? <p className="px-5 text-sm text-danger-ink">{errorText}</p> : <Loader2 className="animate-spin" aria-hidden />}
          <span className="sr-only">{t("loading")}</span>
        </div>
      </div>
    );
  }

  const past = data.interviews.filter((i) => !OPEN_STATUSES.has(i.status) && i.can_record_outcome);

  return (
    <section aria-labelledby={`${ids}-title`} className="flex h-full min-h-0 flex-col" data-scheduling-panel={applicationId}>
      <PanelHeader
        id={`${ids}-title`}
        title={t("title")}
        eyebrow={`${data.job_title} · ${locale === "ar" && data.stage_label_ar ? data.stage_label_ar : data.stage_label}`}
        subtitle={data.label}
        onClose={onClose}
        closeLabel={t("back")}
      />
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
        {data.identity_hidden && (
          <p role="note" className="flex items-start gap-2 rounded-md border bg-surface-sunken/60 px-3 py-2 text-sm">
            <EyeOff className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
            {t("blindNote", { label: data.label })}
          </p>
        )}

        {active && (
          <CurrentInterview
            interview={active}
            viewerTz={viewerTz}
            busy={busy}
            onEdit={() => setEditing((v) => !v)}
            onCancel={() => setConfirmCancel(true)}
            onResend={() => void sendLink()}
          />
        )}

        {active && editing && (
          <EditForm
            interview={active}
            data={data}
            viewerTz={viewerTz}
            busy={busy}
            onClose={() => setEditing(false)}
            onSave={async (patch) => {
              setBusy(true);
              const result = await actions.updateInterview(active.id, patch);
              setBusy(false);
              if (!result.ok) return fail(result.code);
              setPanel(result.interview.panel.map((p) => p.id));
              setDuration(result.interview.duration_min);
              setCandidateTz(result.interview.candidate_timezone);
              return done(t("saved"), result.interview);
            }}
          />
        )}

        <fieldset className="space-y-2">
          <legend className="eyebrow">{t("panel")}</legend>
          <ul className="flex flex-wrap gap-1.5">
            {data.team.map((member) => {
              const on = panel.includes(member.id);
              return (
                <li key={member.id}>
                  <button
                    type="button"
                    aria-pressed={on}
                    data-panelist={member.id}
                    onClick={() =>
                      setPanel((all) => (on ? all.filter((p) => p !== member.id) : [...all, member.id].slice(0, 6)))
                    }
                    title={member.timezone}
                    className={cn(
                      "rounded-full border px-2.5 py-0.5 text-xs",
                      on ? "border-primary bg-primary text-primary-foreground" : "border-input bg-surface hover:bg-accent",
                    )}
                  >
                    {member.name}
                    <span className="ms-1 opacity-70" dir="ltr">
                      {clock(new Date(), member.timezone)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="text-xs text-muted-foreground">{t("panelHelp")}</p>
        </fieldset>

        <div className="grid grid-cols-2 gap-3">
          <FieldRow label={t("duration")} htmlFor={`${ids}-duration`}>
            <Select
              id={`${ids}-duration`}
              value={String(duration)}
              onChange={(e) => setDuration(Number(e.target.value))}
              className="h-9 text-sm"
            >
              {[30, 45, 60, 90].map((m) => (
                <option key={m} value={m}>
                  {t("minutes", { count: m })}
                </option>
              ))}
            </Select>
          </FieldRow>
          <FieldRow label={t("candidateZone")} htmlFor={`${ids}-tz`}>
            <TimeZoneSelect id={`${ids}-tz`} value={candidateTz} onChange={setCandidateTz} />
          </FieldRow>
        </div>
        <p className="text-xs text-muted-foreground">{t("yourZone", { zone: viewerTz })}</p>

        {errorText && (
          <p role="alert" className="text-sm text-danger-ink">
            {errorText}
          </p>
        )}

        {pending && (
          <div role="alert" className="space-y-2 rounded-md border border-signal bg-signal-soft/60 px-3 py-2 text-sm" data-conflicts>
            <p className="flex items-center gap-2 font-medium">
              <TriangleAlert className="size-4" aria-hidden />
              {t("conflictsTitle")}: {whenLabel(pending.startsAt, viewerTz, locale)}
            </p>
            <ul className="list-disc ps-5">
              {pending.check.conflicts.map((c, i) => (
                <li key={`${c.user_id ?? "all"}-${i}`}>
                  {t("conflictLine", {
                    name: c.name ?? t("everyone"),
                    reason: t(`reason.${c.reason}` as "reason.busy"),
                  })}
                </li>
              ))}
            </ul>
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => void choose(pending.startsAt, true)} disabled={busy}>
                {active ? t("moveAnyway") : t("bookAnyway")}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setPending(null)}>
                {t("dismiss")}
              </Button>
            </div>
          </div>
        )}

        <section aria-labelledby={`${ids}-slots`} className="space-y-2">
          <h3 id={`${ids}-slots`} className="eyebrow">
            {t("suggested")}
          </h3>
          {loadingSlots && !slots ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              {t("loadingSlots")}
            </p>
          ) : slots && slots.slots.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("noSlots")}</p>
          ) : (
            <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2" aria-busy={loadingSlots}>
              {(slots?.slots ?? []).map((slot) => {
                const when = whenLabel(slot.start, viewerTz, locale);
                const theirs = clock(slot.start, candidateTz);
                return (
                  <li key={slot.start}>
                    <button
                      type="button"
                      disabled={busy || panel.length === 0}
                      onClick={() => void choose(slot.start)}
                      data-suggested-slot={slot.start}
                      aria-label={
                        active && ACTIVE_STATUSES.has(active.status)
                          ? t("moveHere", { when })
                          : t("bookSlot", { when, candidate: theirs })
                      }
                      className="flex w-full flex-col items-start rounded-md border border-input bg-surface px-3 py-2 text-start text-sm hover:border-foreground/40 hover:bg-accent disabled:opacity-50"
                    >
                      <span className="font-medium" dir="auto">
                        {when}
                      </span>
                      <span className={cn("text-xs text-muted-foreground", !slot.sociable && "text-signal-foreground dark:text-signal")}>
                        {t("candidateTime", { time: theirs })}
                        {!slot.sociable && ` · ${t("unsociable")}`}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {(!active || active.status === "proposed") && (
            <div className="space-y-1 pt-1">
              <Button type="button" size="sm" variant="outline" onClick={() => void sendLink()} disabled={busy || panel.length === 0} data-send-link>
                <Send aria-hidden className="rtl:-scale-x-100" />
                {active ? t("resend") : t("sendLink")}
              </Button>
              <p className="text-xs text-muted-foreground">{t("sendLinkHelp")}</p>
            </div>
          )}
        </section>

        {slots && (
          <WeekView
            slots={slots}
            interview={active && ACTIVE_STATUSES.has(active.status) ? active : null}
            viewerTz={viewerTz}
            duration={duration}
            busy={busy}
            onPick={(startsAt) => void tryTime(startsAt)}
          />
        )}

        {past.length > 0 && (
          <section className="space-y-2">
            <h3 className="eyebrow">{t("outcome")}</h3>
            {past.map((interview) => (
              <div key={interview.id} className="flex flex-wrap items-center gap-2 text-sm" data-past-interview={interview.id}>
                <span>{interview.starts_at ? whenLabel(interview.starts_at, viewerTz, locale) : ""}</span>
                <Badge variant="outline">{t(`status.${interview.status}` as "status.booked")}</Badge>
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void outcome(interview, "completed")}>
                  <UserCheck aria-hidden />
                  {t("completed")}
                </Button>
                <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void outcome(interview, "no_show")}>
                  <UserX aria-hidden />
                  {t("noShow")}
                </Button>
              </div>
            ))}
          </section>
        )}
        {!data.google_enabled && <p className="text-xs text-muted-foreground">{t("demoCalendar")}</p>}
      </div>

      <ConfirmDialog
        open={confirmCancel}
        onOpenChange={setConfirmCancel}
        title={t("cancelTitle")}
        description={t("cancelBody")}
        confirmLabel={t("cancelConfirm")}
        tone="destructive"
        pending={busy}
        onConfirm={() => void cancel()}
      />
    </section>
  );

}

function CurrentInterview({
  interview,
  viewerTz: tz,
  busy: working,
  onEdit,
  onCancel,
  onResend,
}: {
  interview: Interview;
  viewerTz: string;
  busy: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onResend: () => void;
}) {
  const t = useTranslations("Scheduling");
  const locale = useLocale();
  return (
    <div className="space-y-2 card-hairline px-4 py-3 text-sm" data-current-interview={interview.id} data-status={interview.status}>
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-2 font-medium">
          <CalendarClock className="size-4 text-primary" aria-hidden />
          {interview.starts_at ? whenLabel(interview.starts_at, tz, locale) : t("current")}
        </p>
        <Badge variant="default" data-interview-status>
          {t(`status.${interview.status}` as "status.booked")}
        </Badge>
      </div>
      {interview.starts_at ? (
        <p className="text-xs text-muted-foreground">
          {t("candidateTime", { time: `${clock(interview.starts_at, interview.candidate_timezone)} (${interview.candidate_timezone})` })}
        </p>
      ) : (
        interview.link_expires_at && (
          <p className="text-xs text-muted-foreground">
            {t("waiting", { date: whenLabel(interview.link_expires_at, tz, locale) })}
          </p>
        )
      )}
      <p className="text-xs">{interview.panel.map((p) => p.name).join(", ")}</p>
      {interview.starts_at &&
        (interview.meeting_url ? (
          <a href={interview.meeting_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-primary underline">
            <Video className="size-3.5" aria-hidden />
            {t("join")}
          </a>
        ) : (
          <p className="text-xs text-muted-foreground">{t("noMeeting")}</p>
        ))}
      <div className="flex flex-wrap gap-2 pt-1">
        <Button type="button" size="sm" variant="outline" onClick={onEdit} disabled={working} data-edit-interview>
          <Pencil aria-hidden />
          {t("edit")}
        </Button>
        {interview.status === "proposed" && (
          <Button type="button" size="sm" variant="ghost" onClick={onResend} disabled={working}>
            <Link2 aria-hidden />
            {t("resend")}
          </Button>
        )}
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={working} data-cancel-interview>
          <CalendarX2 aria-hidden />
          {t("cancel")}
        </Button>
      </div>
    </div>
  );
}

function EditForm({
  interview,
  data: panelData,
  viewerTz: tz,
  busy: working,
  onClose: closeForm,
  onSave,
}: {
  interview: Interview;
  data: SchedulingPanelData;
  viewerTz: string;
  busy: boolean;
  onClose: () => void;
  onSave: (patch: Parameters<SchedulingActions["updateInterview"]>[1]) => Promise<void>;
}) {
  const t = useTranslations("Scheduling");
  const ids = useId();
  const initialLocal = interview.starts_at
    ? `${localDate(interview.starts_at, tz)}T${clock(interview.starts_at, tz)}`
    : "";
  const [when, setWhen] = useState(initialLocal);
  const [minutes, setMinutes] = useState(interview.duration_min);
  const [people, setPeople] = useState(interview.panel.map((p) => p.id));
  const [link, setLink] = useState(interview.meeting_provider === "manual" ? (interview.meeting_url ?? "") : "");
  const [zone, setZone] = useState(interview.candidate_timezone);
  return (
    <form
      className="space-y-3 card-hairline px-4 py-3"
      aria-label={t("form.title")}
      data-edit-form
      onSubmit={(e) => {
        e.preventDefault();
        const patch: Parameters<SchedulingActions["updateInterview"]>[1] = {};
        if (when && when !== initialLocal) {
          const [date, time] = when.split("T");
          const [h, m] = (time ?? "00:00").split(":").map(Number);
          patch.startsAt = instantAt(date ?? "", (h ?? 0) * 60 + (m ?? 0), tz).toISOString();
        }
        if (minutes !== interview.duration_min) patch.durationMin = minutes;
        const before = interview.panel.map((p) => p.id);
        if (people.length > 0 && (people.length !== before.length || people.some((p) => !before.includes(p)))) {
          patch.panel = people;
        }
        if (link.trim()) patch.meetingUrl = link.trim();
        else if (interview.meeting_provider === "manual") patch.clearMeetingUrl = true;
        if (zone !== interview.candidate_timezone) patch.candidateTimezone = zone;
        void onSave(patch);
      }}
    >
      <p className="font-medium">{t("form.title")}</p>
      <FieldRow label={t("form.time", { zone: tz })} htmlFor={`${ids}-when`}>
        <Input id={`${ids}-when`} type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="h-9" />
      </FieldRow>
      <div className="grid grid-cols-2 gap-3">
        <FieldRow label={t("form.duration")} htmlFor={`${ids}-minutes`}>
          <Input
            id={`${ids}-minutes`}
            type="number"
            min={15}
            max={240}
            step={5}
            value={minutes}
            onChange={(e) => setMinutes(Number(e.target.value))}
            className="h-9"
          />
        </FieldRow>
        <FieldRow label={t("form.candidateZone")} htmlFor={`${ids}-zone`}>
          <TimeZoneSelect id={`${ids}-zone`} value={zone} onChange={setZone} />
        </FieldRow>
      </div>
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium">{t("form.panel")}</legend>
        {panelData.team.map((member) => (
          <label key={member.id} className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={people.includes(member.id)}
              onChange={(e) =>
                setPeople((all) => (e.target.checked ? [...all, member.id] : all.filter((p) => p !== member.id)))
              }
            />
            {member.name} <span className="text-xs text-muted-foreground">{member.timezone}</span>
          </label>
        ))}
      </fieldset>
      <FieldRow label={t("form.meetingLink")} htmlFor={`${ids}-link`} hint={t("form.meetingHelp")}>
        <Input
          id={`${ids}-link`}
          type="url"
          inputMode="url"
          dir="ltr"
          placeholder="https://"
          value={link}
          onChange={(e) => setLink(e.target.value)}
          className="h-9"
        />
      </FieldRow>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={working}>
          {t("form.save")}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={closeForm}>
          {t("form.close")}
        </Button>
      </div>
    </form>
  );
}


function PanelHeader({
  id,
  title,
  eyebrow,
  subtitle,
  onClose,
  closeLabel,
}: {
  id?: string;
  title: string;
  eyebrow?: string;
  subtitle?: string;
  onClose: () => void;
  closeLabel: string;
}) {
  return (
    <header className="flex items-start justify-between gap-3 border-b px-5 pt-5 pb-4">
      <div className="min-w-0 space-y-1">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h2 id={id} className="font-display text-2xl leading-tight">
          {title}
        </h2>
        {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={closeLabel}>
        <X aria-hidden />
      </Button>
    </header>
  );
}

const COMMON_ZONES = [
  "Asia/Dubai",
  "Asia/Riyadh",
  "Asia/Qatar",
  "Africa/Cairo",
  "Asia/Beirut",
  "Asia/Amman",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Asia/Kolkata",
  "Asia/Karachi",
  "Asia/Singapore",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "UTC",
];

export function TimeZoneSelect({
  id,
  value,
  onChange,
  className,
}: {
  id?: string;
  value: string;
  onChange: (zone: string) => void;
  className?: string;
}) {
  const zones = COMMON_ZONES.includes(value) ? COMMON_ZONES : [value, ...COMMON_ZONES];
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} className={cn("h-9 text-sm", className)} dir="ltr">
      {zones.map((zone) => (
        <option key={zone} value={zone}>
          {zone.replaceAll("_", " ")}
        </option>
      ))}
    </Select>
  );
}

const DAY_START = 7 * 60;
const DAY_END = 20 * 60;
const DAYS_SHOWN = 5;

/**
 * A small calendar of the panel (viewer's time zone): busy time shaded, free starts as
 * drop targets, the booked interview draggable. Arrow keys move the interview (up and
 * down by a step, left and right by a day) and Enter asks for that time.
 */
function WeekView({
  slots,
  interview,
  viewerTz,
  duration,
  busy,
  onPick,
}: {
  slots: SlotsResult;
  interview: Interview | null;
  viewerTz: string;
  duration: number;
  busy: boolean;
  onPick: (startsAt: string) => void;
}) {
  const t = useTranslations("Scheduling");
  const locale = useLocale();
  const step = 30;
  const firstDay = localDate(interview?.starts_at ?? slots.slots[0]?.start ?? slots.window_start, viewerTz);
  const [from, setFrom] = useState(firstDay);
  const days = nextDates(from, DAYS_SHOWN);
  const free = useMemo(() => new Set(slots.free.map((s) => Date.parse(s))), [slots.free]);
  const blocks = useMemo(() => slots.busy.flatMap((b) => b.blocks), [slots.busy]);
  const [cursor, setCursor] = useState<{ date: string; minutes: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const refocus = useRef(false);

  useEffect(() => {
    // The block renders in its new cell after a key move: keep the keyboard on it.
    if (!refocus.current) return;
    refocus.current = false;
    document.querySelector<HTMLElement>("[data-interview-block]")?.focus();
  }, [cursor]);
  const rows = Array.from({ length: (DAY_END - DAY_START) / step }, (_, i) => DAY_START + i * step);
  const ivDate = interview?.starts_at ? localDate(interview.starts_at, viewerTz) : null;
  const ivMinutes = interview?.starts_at ? minutesOfDay(interview.starts_at, viewerTz) : null;
  const shown = cursor ?? (ivDate !== null && ivMinutes !== null ? { date: ivDate, minutes: ivMinutes } : null);

  const startOf = (date: string, minutes: number) => instantAt(date, minutes, viewerTz).toISOString();

  return (
    <section className="space-y-2" aria-label={t("calendar")} data-week-view>
      <div className="flex items-center justify-between">
        <h3 className="eyebrow">{t("calendar")}</h3>
        <div className="flex gap-1">
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={t("prevWeek")}
            onClick={() => setFrom(addDays(from, -DAYS_SHOWN))}
            className="size-7"
          >
            <ChevronLeft className="rtl:rotate-180" aria-hidden />
          </Button>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={t("nextWeek")}
            onClick={() => setFrom(addDays(from, DAYS_SHOWN))}
            className="size-7"
          >
            <ChevronRight className="rtl:rotate-180" aria-hidden />
          </Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{t("calendarHelp")}</p>
      <div className="grid gap-px overflow-hidden rounded-md border bg-border text-[0.65rem]" style={{ gridTemplateColumns: `2.5rem repeat(${DAYS_SHOWN}, minmax(0, 1fr))` }}>
        <div className="bg-background" />
        {days.map((date) => (
          <div key={date} className="bg-background px-1 py-1 text-center font-medium" dir="auto">
            {dayLabel(date, locale)}
          </div>
        ))}
        {rows.map((minutes) => row(minutes))}
      </div>
    </section>
  );

  function row(minutes: number) {
    return (
      <Fragment key={minutes}>
        <div className="bg-background pe-1 text-end text-muted-foreground tabular-nums" dir="ltr">
          {minutes % 60 === 0 ? `${String(minutes / 60).padStart(2, "0")}:00` : ""}
        </div>
        {days.map((date) => {
          const start = Date.parse(startOf(date, minutes));
          const end = start + step * 60_000;
          const isBusy = blocks.some((b) => overlaps(b, start, end));
          const isFree = free.has(start);
          const here = shown !== null && shown.date === date && shown.minutes === minutes;
          const covered =
            shown !== null && shown.date === date && minutes > shown.minutes && minutes < shown.minutes + duration;
          return (
            <div
              key={date}
              className={cn(
                "relative h-5 bg-background",
                isBusy && "bg-muted",
                isFree && "bg-success/10",
                dragging && isFree && "outline outline-1 outline-success/40",
                covered && "bg-primary/20",
              )}
              data-free-cell={isFree ? new Date(start).toISOString() : undefined}
              title={isBusy ? t("busy") : isFree ? t("free") : undefined}
              onDragOver={(e) => {
                if (!interview) return;
                e.preventDefault();
              }}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                if (!interview || busy) return;
                setCursor({ date, minutes });
                onPick(new Date(start).toISOString());
              }}
            >
              {here && interview && (
                <button
                  type="button"
                  draggable={!busy}
                  data-interview-block={interview.id}
                  aria-label={t("interviewBlock", { when: whenLabel(startOf(date, minutes), viewerTz, locale) })}
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/x-pairly-interview", interview.id);
                    e.dataTransfer.effectAllowed = "move";
                    setDragging(true);
                  }}
                  onDragEnd={() => setDragging(false)}
                  onKeyDown={(e) => {
                    const at = shown ?? { date, minutes };
                    const rtl = document.dir === "rtl";
                    let next: { date: string; minutes: number } | null = null;
                    if (e.key === "ArrowUp") next = { ...at, minutes: Math.max(DAY_START, at.minutes - step) };
                    else if (e.key === "ArrowDown") next = { ...at, minutes: Math.min(DAY_END - step, at.minutes + step) };
                    else if (e.key === (rtl ? "ArrowLeft" : "ArrowRight"))
                      next = { ...at, date: addDays(at.date, 1) };
                    else if (e.key === (rtl ? "ArrowRight" : "ArrowLeft"))
                      next = { ...at, date: addDays(at.date, -1) };
                    else if (e.key === "Enter" && cursor) {
                      e.preventDefault();
                      onPick(startOf(cursor.date, cursor.minutes));
                      return;
                    } else if (e.key === "Escape") {
                      setCursor(null);
                      return;
                    }
                    if (next) {
                      e.preventDefault();
                      refocus.current = true;
                      setCursor(next);
                    }
                  }}
                  className="absolute inset-x-0 top-0 z-10 rounded-sm bg-primary px-1 text-start text-primary-foreground shadow-sm focus-visible:ring-2 focus-visible:ring-ring"
                  style={{ height: `${(duration / step) * 1.25 + (duration / step - 1) * 0.0625}rem` }}
                >
                  {clock(startOf(date, minutes), viewerTz)}
                </button>
              )}
            </div>
          );
        })}
      </Fragment>
    );
  }
}

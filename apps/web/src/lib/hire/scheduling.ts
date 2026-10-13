// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Interview scheduling shapes (generated from the OpenAPI schema, `pnpm gen:api`) and the
 * small pure helpers the scheduling panel and the public scheduling page share: time
 * formatting in a given zone, grouping slots per local day, the small calendar's grid.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type SchedulingPanelData = S["HireSchedulingPanelOut"];
export type Interview = S["HireInterviewOut"];
export type PanelMember = S["HirePanelMemberOut"];
export type SlotsResult = S["HireSlotsOut"];
export type Slot = S["HireSlotOut"];
export type SlotCheck = S["HireCheckOut"];
export type CalendarIntegrations = S["HireCalendarIntegrationsOut"];
export type CalendarProviderStatus = S["HireCalendarProviderOut"];
export type OrgIntegrations = S["HireOrgIntegrationsOut"];
export type OrgIntegration = S["HireOrgIntegrationOut"];
export type CalendarOperations = S["HireCalendarOperationsOut"];
export type Availability = S["HireAvailabilityOut"];
export type PublicSchedule = S["HirePublicScheduleOut"];
export type PublicSlot = S["HirePublicSlotOut"];

/** Interview statuses that still hold or wait for a time. */
export const OPEN_STATUSES = new Set(["proposed", "booked", "rescheduled"]);
export const ACTIVE_STATUSES = new Set(["booked", "rescheduled"]);

/** The browser's own IANA zone (UTC when the browser does not say). */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function isTimeZone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/** `HH:MM` of an instant in a zone (Latin digits, 24 hours). */
export function clock(iso: string | Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(
    new Date(iso),
  );
}

/** `YYYY-MM-DD` of an instant in a zone. */
export function localDate(iso: string | Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(iso),
  );
  return parts;
}

/** A day heading such as `Tue 13 Oct` in the reader's language. */
export function dayLabel(date: string, locale: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const at = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1, 12));
  return new Intl.DateTimeFormat(locale === "ar" ? "ar-u-nu-latn" : "en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(at);
}

/** A full time: `Tue 13 Oct, 10:00` in a zone. */
export function whenLabel(iso: string, timeZone: string, locale: string): string {
  return `${dayLabel(localDate(iso, timeZone), locale)}, ${clock(iso, timeZone)}`;
}

/** Slots grouped by their local date in `timeZone`, in order. */
export function groupByDay<T extends { start: string }>(slots: T[], timeZone: string): { date: string; slots: T[] }[] {
  const days = new Map<string, T[]>();
  for (const slot of slots) {
    const date = localDate(slot.start, timeZone);
    const list = days.get(date) ?? [];
    list.push(slot);
    days.set(date, list);
  }
  return [...days.entries()].map(([date, items]) => ({ date, slots: items }));
}

/** Minutes since local midnight of an instant in a zone. */
export function minutesOfDay(iso: string | Date, timeZone: string): number {
  const [h, m] = clock(iso, timeZone).split(":").map(Number);
  return (h ?? 0) * 60 + (m ?? 0);
}

/**
 * The instant at `date` (YYYY-MM-DD) `minutes` after local midnight in `timeZone`. Found by
 * correcting a UTC guess with the zone's offset at that moment (twice, so a guess on the
 * other side of a clock change settles).
 */
export function instantAt(date: string, minutes: number, timeZone: string): Date {
  const [y, m, d] = date.split("-").map(Number);
  const wall = Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1, Math.floor(minutes / 60), minutes % 60);
  let guess = wall;
  for (let i = 0; i < 2; i += 1) {
    const shown = new Date(guess);
    const asWall = Date.UTC(
      Number(localDate(shown, timeZone).slice(0, 4)),
      Number(localDate(shown, timeZone).slice(5, 7)) - 1,
      Number(localDate(shown, timeZone).slice(8, 10)),
      Math.floor(minutesOfDay(shown, timeZone) / 60),
      minutesOfDay(shown, timeZone) % 60,
    );
    guess += wall - asWall;
  }
  return new Date(guess);
}

/** `date` (YYYY-MM-DD) moved by `days` (negative goes back). */
export function addDays(date: string, days: number): string {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, (d ?? 1) + days)).toISOString().slice(0, 10);
}

/** The next `count` local dates from `from` (YYYY-MM-DD), skipping nothing. */
export function nextDates(from: string, count: number): string[] {
  const [y, m, d] = from.split("-").map(Number);
  const start = Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1);
  return Array.from({ length: count }, (_, i) => new Date(start + i * 86_400_000).toISOString().slice(0, 10));
}

export function overlaps(a: { start: string; end: string }, startMs: number, endMs: number): boolean {
  return Date.parse(a.start) < endMs && startMs < Date.parse(a.end);
}

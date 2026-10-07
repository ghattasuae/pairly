// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Helpers for the notification preferences form. Pure, no I/O. */

export const SNOOZE_OPTIONS = ["1h", "4h", "tomorrow", "week"] as const;
export type SnoozeOption = (typeof SNOOZE_OPTIONS)[number];

/** Minutes after midnight as `HH:MM` (the API stores quiet hours in minutes). */
export function minutesToTime(minutes: number | null): string {
  if (minutes === null) return "";
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function timeToMinutes(value: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return Number(match[2]) < 60 && minutes >= 0 && minutes < 1440 ? minutes : null;
}

/** When a snooze choice ends, from now. "Tomorrow" is 09:00 tomorrow in the browser's time zone. */
export function snoozeEnd(choice: SnoozeOption, now = new Date()): Date {
  const end = new Date(now);
  if (choice === "1h") end.setHours(end.getHours() + 1);
  else if (choice === "4h") end.setHours(end.getHours() + 4);
  else if (choice === "week") end.setDate(end.getDate() + 7);
  else {
    end.setDate(end.getDate() + 1);
    end.setHours(9, 0, 0, 0);
  }
  return end;
}

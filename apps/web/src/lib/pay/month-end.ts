// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** The month-end page's period (validated before the API). Pure. */
const PERIOD_RE = /^(\d{4})-(0[1-9]|1[0-2])$/;

/** The month before `today`'s, as YYYY-MM. */
export function previousPeriod(today: Date): string {
  const year = today.getUTCMonth() === 0 ? today.getUTCFullYear() - 1 : today.getUTCFullYear();
  const month = today.getUTCMonth() === 0 ? 12 : today.getUTCMonth();
  return `${year}-${String(month).padStart(2, "0")}`;
}

/** The period from the URL (YYYY-MM, 2000 to 2100), else the previous month. */
export function monthEndPeriod(raw: Record<string, string | string[] | undefined>, today: Date = new Date()): string {
  const value = typeof raw.period === "string" ? raw.period.trim() : "";
  const match = PERIOD_RE.exec(value);
  if (!match) return previousPeriod(today);
  const year = Number(match[1]);
  return year >= 2000 && year <= 2100 ? value : previousPeriod(today);
}

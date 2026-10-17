// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** The cash forecast page's query (validated before it reaches the API). Pure. */
export const FORECAST_WEEKS = 12;
export const MAX_FORECAST_WEEKS = 26;

/** Weeks from the URL: a whole number from 1 to 26, else the default 12. */
export function forecastWeeks(raw: Record<string, string | string[] | undefined>): number {
  const value = typeof raw.weeks === "string" ? raw.weeks.trim() : "";
  if (!/^\d{1,2}$/.test(value)) return FORECAST_WEEKS;
  const weeks = Number(value);
  return weeks >= 1 && weeks <= MAX_FORECAST_WEEKS ? weeks : FORECAST_WEEKS;
}

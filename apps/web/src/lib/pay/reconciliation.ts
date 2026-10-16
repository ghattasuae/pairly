// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** The reconciliation page's query and file kind (validated before the API). Pure. */
export const RECONCILIATION_DAYS = 5;
export const MAX_RECONCILIATION_DAYS = 90;

/** Days from the URL: a whole number from 1 to 90, else the default 5. */
export function reconciliationDays(raw: Record<string, string | string[] | undefined>): number {
  const value = typeof raw.days === "string" ? raw.days.trim() : "";
  if (!/^\d{1,2}$/.test(value)) return RECONCILIATION_DAYS;
  const days = Number(value);
  return days >= 1 && days <= MAX_RECONCILIATION_DAYS ? days : RECONCILIATION_DAYS;
}

/** The statement format from a file name: camt.053 for XML, else CSV. */
export function statementFormat(fileName: string): "csv" | "camt053" {
  return /\.xml$/i.test(fileName.trim()) ? "camt053" : "csv";
}

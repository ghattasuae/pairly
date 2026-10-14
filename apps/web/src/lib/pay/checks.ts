// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pure helpers for the invoice checks (WP4.6): the findings in the order a reviewer
 * should read them, and the rows of the side-by-side comparison with a suspected
 * original. No I/O.
 */
import type { PayCheck, PayCheckFinding, PayCompare, PayCompareSide } from "./types";

export const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;

export function severityRank(severity: string): number {
  const index = (SEVERITIES as readonly string[]).indexOf(severity);
  return index === -1 ? SEVERITIES.length : index;
}

export interface ShownFinding extends PayCheckFinding {
  check: string;
}

/** Every finding of every check, worst first (stable within a severity). */
export function sortedFindings(checks: readonly PayCheck[]): ShownFinding[] {
  return checks
    .flatMap((c) => c.findings.map((f) => ({ ...f, check: c.check })))
    .map((f, i) => ({ f, i }))
    .sort((a, b) => severityRank(a.f.severity) - severityRank(b.f.severity) || a.i - b.i)
    .map(({ f }) => f);
}

/** The checks that could not run (they send the invoice to a person). */
export function failedChecks(checks: readonly PayCheck[]): PayCheck[] {
  return checks.filter((c) => c.status === "error");
}

/** The finding's sentence in the reader's language (English when Arabic is missing). */
export function findingMessage(finding: Pick<PayCheckFinding, "message">, locale: string): string {
  const ar = finding.message.ar;
  return locale === "ar" && ar ? ar : (finding.message.en ?? "");
}

export const COMPARE_FIELDS = [
  "invoice_number",
  "invoice_date",
  "due_date",
  "currency",
  "subtotal",
  "vat_amount",
  "total",
  "po_number",
  "vendor_name",
] as const;
export type CompareField = (typeof COMPARE_FIELDS)[number];

export interface CompareRow {
  field: CompareField;
  invoice: string | null;
  original: string | null;
  same: boolean;
}

function show(value: unknown): string | null {
  return value === null || value === undefined || value === "" ? null : String(value);
}

/** One row per header field, marking where the two invoices differ. */
export function compareRows(pair: Pick<PayCompare, "invoice" | "original">): CompareRow[] {
  return COMPARE_FIELDS.map((field) => {
    const invoice = show(pair.invoice[field as keyof PayCompareSide]);
    const original = show(pair.original[field as keyof PayCompareSide]);
    return { field, invoice, original, same: invoice === original };
  });
}

/** Line items side by side (the longer list sets the number of rows). */
export function compareLines(pair: Pick<PayCompare, "invoice" | "original">) {
  const count = Math.max(pair.invoice.lines.length, pair.original.lines.length);
  return Array.from({ length: count }, (_, i) => {
    const a = pair.invoice.lines[i] ?? null;
    const b = pair.original.lines[i] ?? null;
    const same =
      a !== null &&
      b !== null &&
      a.description === b.description &&
      a.quantity === b.quantity &&
      a.unit_price === b.unit_price &&
      a.line_total === b.line_total;
    return { index: i, invoice: a, original: b, same };
  });
}

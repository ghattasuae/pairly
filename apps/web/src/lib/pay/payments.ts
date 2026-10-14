// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pure helpers for invoice approval and payment runs (WP4.7). No I/O.
 *
 * Reasons from the API look like `vendor_held:manual` or, for a bank file,
 * `PR-ABC123-0001:bank_details_changed`. The screens translate the code and keep the
 * detail as it is.
 */

export const RUN_STATUSES = [
  "awaiting_approval",
  "approved",
  "exported",
  "paid",
  "rejected",
  "cancelled",
  "failed",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && (RUN_STATUSES as readonly string[]).includes(value);
}

/** The status filter from a search parameter, or null for "all". */
export function runStatusFilter(value: string | string[] | undefined): RunStatus | null {
  const first = Array.isArray(value) ? value[0] : value;
  return isRunStatus(first) ? first : null;
}

/** Codes the screens explain in words (the rest is shown as it is). */
export const BLOCKER_CODES = [
  "no_vendor",
  "vendor_not_active",
  "vendor_held",
  "bank_change_open",
  "no_bank_details",
  "checks_stale",
  "checks_failed",
  "checks_missing",
  "amount_missing",
  "currency_missing",
  "not_approved",
  "bank_details_changed",
  "checks_changed_since_approval",
  "amount_changed",
  "currency_changed",
  "invoice_not_in_this_run",
  "invoice_missing",
  "over_cash_limit",
  "not_due_yet",
  "run_full",
  "nothing_left_to_pay",
  "payer_details_missing",
  "fully_credited",
  "discount_changed",
] as const;
export type BlockerCode = (typeof BLOCKER_CODES)[number];

export interface Reason {
  /** The translated code, or null when the screens do not know it. */
  code: BlockerCode | null;
  /** What came after the code (`manual` in `vendor_held:manual`), if anything. */
  detail: string | null;
  /** The bank file item it is about (`PR-ABC123-0001`), if any. */
  item: string | null;
  raw: string;
}

const ITEM_RE = /^([A-Z]{2}-[A-Z0-9]{4,12}-\d{4}):(.+)$/;

/** Split a reason into its item, code and detail. */
export function parseReason(raw: string): Reason {
  const itemMatch = ITEM_RE.exec(raw);
  const item = itemMatch ? (itemMatch[1] ?? null) : null;
  const rest = itemMatch ? (itemMatch[2] ?? "") : raw;
  const colon = rest.indexOf(":");
  const head = colon < 0 ? rest : rest.slice(0, colon);
  const detail = colon < 0 ? null : rest.slice(colon + 1) || null;
  const code = (BLOCKER_CODES as readonly string[]).includes(head) ? (head as BlockerCode) : null;
  return { code, detail, item, raw };
}

export type ChecksState = "current" | "stale" | "failed" | "missing";

/**
 * Whether a person may send the invoice for approval now, and if not, why. The API
 * decides again; this only keeps the button honest.
 */
export function submitState(invoice: {
  status: string;
  checks_state?: ChecksState | null | undefined;
  blockers?: readonly string[] | null | undefined;
}): { can: boolean; why: string[] } {
  if (invoice.status !== "ready") return { can: false, why: ["not_ready"] };
  const why = [...(invoice.blockers ?? [])];
  if ((invoice.checks_state ?? "missing") !== "current" && !why.some((w) => w.startsWith("checks_"))) {
    why.push(`checks_${invoice.checks_state ?? "missing"}`);
  }
  return { can: why.length === 0, why };
}

/** A decimal string as a number of minor units, for totals without float drift. */
export function toMinor(value: string | number | null | undefined, places = 2): bigint | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  const match = /^(-)?(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) return null;
  const [, sign, whole, frac = ""] = match;
  const padded = (frac + "0".repeat(places)).slice(0, places);
  const minor = BigInt(whole ?? "0") * 10n ** BigInt(places) + BigInt(padded || "0");
  return sign ? -minor : minor;
}

/** Minor units back to a decimal string. */
export function fromMinor(minor: bigint, places = 2): string {
  const negative = minor < 0n;
  const abs = negative ? -minor : minor;
  const base = 10n ** BigInt(places);
  const whole = abs / base;
  const frac = (abs % base).toString().padStart(places, "0");
  return `${negative ? "-" : ""}${whole.toString()}${places ? `.${frac}` : ""}`;
}

/** The unpaid and paid sums of a run's items. */
export function runSums(items: readonly { status: string; amount: string | number }[]): {
  unpaid: string;
  paid: string;
} {
  let unpaid = 0n;
  let paid = 0n;
  for (const item of items) {
    const minor = toMinor(item.amount) ?? 0n;
    if (item.status === "paid") paid += minor;
    else if (item.status === "included") unpaid += minor;
  }
  return { unpaid: fromMinor(unpaid), paid: fromMinor(paid) };
}

/** The file name the browser saves an export as. */
export function exportFilename(reference: string, format: "csv" | "pain001", count: number): string {
  const safe = reference.replace(/[^A-Za-z0-9-]/g, "");
  return `${safe}-${count}.${format === "csv" ? "csv" : "xml"}`;
}

/** Parts of an approval rules YAML document for a read-only view: comments and the rest. */
export function rulesLines(content: string): { text: string; comment: boolean }[] {
  return content.split(/\r?\n/).map((text) => ({ text, comment: /^\s*#/.test(text) }));
}

/** Chain names declared in a rules document, in order (for a quick summary). */
export function chainNames(content: string): string[] {
  const names: string[] = [];
  let inChains = false;
  for (const line of content.split(/\r?\n/)) {
    if (/^chains:\s*$/.test(line)) {
      inChains = true;
      continue;
    }
    if (inChains && /^\S/.test(line) && !line.startsWith("#")) inChains = false;
    const match = inChains ? /^\s*-\s*name:\s*([a-z][a-z0-9_]*)\s*$/.exec(line) : null;
    if (match?.[1]) names.push(match[1]);
  }
  return names;
}

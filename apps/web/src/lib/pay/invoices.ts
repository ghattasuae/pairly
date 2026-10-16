// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pure helpers for the invoice list and the side-by-side review (WP4.5). No I/O.
 *
 * Fields come from the API flat, one per leaf path (`total`, `bank.iban`,
 * `line_items[2].unit_price`). The review shows them grouped: the header, the bank
 * details printed on the invoice, then one row per line item. A field "needs a look"
 * when the API says it needs correction (low confidence, no evidence, a value that does
 * not parse, missing) and nobody corrected it yet.
 */

export const INVOICE_STATUSES = [
  "received",
  "extracting",
  "needs_review",
  "ready",
  "awaiting_approval",
  "approved",
  "scheduled",
  "paid",
  "rejected",
] as const;
export type InvoiceStatus = (typeof INVOICE_STATUSES)[number];

export function isInvoiceStatus(value: unknown): value is InvoiceStatus {
  return typeof value === "string" && (INVOICE_STATUSES as readonly string[]).includes(value);
}

/** The status filter from a search parameter, or null for "all". */
export function statusFilter(value: string | string[] | undefined): InvoiceStatus | null {
  const first = Array.isArray(value) ? value[0] : value;
  return isInvoiceStatus(first) ? first : null;
}

export interface ReviewField {
  path: string;
  pattern: string;
  kind: string;
  value: unknown;
  current_value: unknown;
  raw_value: string | null;
  confidence: number;
  threshold: number;
  required: boolean;
  needs_correction: boolean;
  corrected: boolean;
  flags: string[];
  spans: Record<string, unknown>[];
}

export const HEADER_ORDER = [
  "vendor_name",
  "vendor_trn",
  "invoice_number",
  "invoice_date",
  "due_date",
  "payment_terms",
  "currency",
  "po_number",
  "subtotal",
  "vat_rate",
  "vat_amount",
  "total",
] as const;

export const LINE_ORDER = ["description", "quantity", "unit_price", "vat_rate", "vat_amount", "line_total"] as const;
export const BANK_ORDER = ["iban", "account_name", "bank_name", "swift"] as const;

export interface FieldGroups {
  header: ReviewField[];
  bank: ReviewField[];
  lines: { index: number; fields: ReviewField[] }[];
}

const LINE_RE = /^line_items\[(\d+)\]\.(\w+)$/;

function byOrder(order: readonly string[], name: (f: ReviewField) => string) {
  return (a: ReviewField, b: ReviewField) => {
    const ia = order.indexOf(name(a));
    const ib = order.indexOf(name(b));
    return (ia < 0 ? order.length : ia) - (ib < 0 ? order.length : ib);
  };
}

/** Header, bank and line items, each in reading order. */
export function groupFields(fields: readonly ReviewField[]): FieldGroups {
  const header: ReviewField[] = [];
  const bank: ReviewField[] = [];
  const lines = new Map<number, ReviewField[]>();
  for (const field of fields) {
    const line = LINE_RE.exec(field.path);
    if (line) {
      const index = Number(line[1]);
      lines.set(index, [...(lines.get(index) ?? []), field]);
    } else if (field.path.startsWith("bank.")) bank.push(field);
    else header.push(field);
  }
  header.sort(byOrder(HEADER_ORDER, (f) => f.path));
  bank.sort(byOrder(BANK_ORDER, (f) => f.path.slice(5)));
  return {
    header,
    bank,
    lines: [...lines.entries()]
      .sort(([a], [b]) => a - b)
      .map(([index, items]) => ({ index, fields: items.sort(byOrder(LINE_ORDER, (f) => fieldName(f.path))) })),
  };
}

/** The last segment of a path: `line_items[1].unit_price` is `unit_price`. */
export function fieldName(path: string): string {
  const dot = path.lastIndexOf(".");
  return dot < 0 ? path : path.slice(dot + 1);
}

/** Needs a person: flagged and not corrected yet. */
export function needsLook(field: ReviewField): boolean {
  return field.needs_correction && !field.corrected;
}

/** Fields in the order a keyboard moves through them (header, bank, lines). */
export function reviewOrder(groups: FieldGroups): ReviewField[] {
  return [...groups.header, ...groups.bank, ...groups.lines.flatMap((l) => l.fields)];
}

/** The next field index for a key, or null when the key does not move. */
export function moveIndex(key: string, current: number, count: number): number | null {
  if (count === 0) return null;
  if (key === "ArrowDown" || key === "j") return Math.min(count - 1, current + 1);
  if (key === "ArrowUp" || key === "k") return Math.max(0, current - 1);
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/** The first field that needs a look, else the first field (where review starts). */
export function firstToReview(order: readonly ReviewField[]): number {
  const index = order.findIndex(needsLook);
  return index < 0 ? 0 : index;
}

/** A value as text for the field list and the edit box. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "object" && value !== null && "amount" in value) {
    const money = value as { amount?: unknown; currency?: unknown };
    return `${String(money.currency ?? "")} ${String(money.amount ?? "")}`.trim();
  }
  return JSON.stringify(value);
}

/** Confidence as a whole percent. */
export function percent(confidence: number): number {
  return Math.round(Math.max(0, Math.min(1, confidence)) * 100);
}

/** The id the document viewer uses for a field's first evidence highlight. */
export function highlightId(path: string): string {
  return `field:${path}:0`;
}

export interface TextPiece {
  text: string;
  /** Paths whose evidence covers this piece. */
  paths: string[];
}

/**
 * Split a document text into pieces by the fields' evidence offsets, so a text view
 * (email body, DOCX, XML) can mark where each value was read. Offsets outside the
 * text are ignored.
 */
export function textPieces(text: string, fields: readonly ReviewField[]): TextPiece[] {
  const cuts = new Set<number>([0, text.length]);
  const ranges: { start: number; end: number; path: string }[] = [];
  for (const field of fields) {
    for (const span of field.spans) {
      const start = span.start;
      const end = span.end;
      if (typeof start !== "number" || typeof end !== "number") continue;
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > text.length || end <= start) continue;
      ranges.push({ start, end, path: field.path });
      cuts.add(start);
      cuts.add(end);
    }
  }
  const points = [...cuts].sort((a, b) => a - b);
  const pieces: TextPiece[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i] ?? 0;
    const end = points[i + 1] ?? start;
    if (end <= start) continue;
    const paths = ranges.filter((r) => r.start <= start && r.end >= end).map((r) => r.path);
    pieces.push({ text: text.slice(start, end), paths });
  }
  return pieces;
}

/** Review reasons as translation keys: the part before the first colon. */
export function reasonKey(reason: string): string {
  const head = reason.split(":", 1)[0] ?? reason;
  return head.replace(/[^a-z_]/g, "");
}

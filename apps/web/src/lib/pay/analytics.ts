// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Helpers for the Pay analytics page (brief 4.9): where each record behind a number
 * opens, how a metric's value reads, and the page's query (validated before it reaches
 * the API). Pure.
 */
import type { PayMetric, PayRecordLink } from "./types";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** The in-app page of one record: an invoice, a bank-detail change, or an approval. */
export function recordHref(record: Pick<PayRecordLink, "kind" | "id">): string | null {
  if (!UUID_RE.test(record.id)) return null;
  switch (record.kind) {
    case "invoice":
      return `/pay/invoices/${record.id}`;
    case "bank_change":
      return `/pay/bank-changes/${record.id}`;
    case "approval":
      return `/inbox?item=${record.id}`;
    default:
      return null;
  }
}

export type ValueParts = { kind: "none" } | { kind: "number"; value: number; unit: string };

/** The value to show, or "none" when there is no data (never a made-up zero). */
export function metricValue(metric: Pick<PayMetric, "value" | "unit">): ValueParts {
  if (metric.value === null || metric.value === undefined || !Number.isFinite(metric.value)) {
    return { kind: "none" };
  }
  return { kind: "number", value: metric.value, unit: metric.unit };
}

/** The part of a metric key after its group (`exceptions.check.bank` -> `check.bank`). */
export function keyTail(key: string): string {
  const dot = key.indexOf(".");
  return dot === -1 ? key : key.slice(dot + 1);
}

export interface PayAnalyticsQuery {
  since?: string;
  until?: string;
}

function one(value: string | string[] | undefined): string | undefined {
  return typeof value === "string" ? value.trim() : undefined;
}

/** The page's filters from the URL; anything invalid is dropped, never sent as is. */
export function payAnalyticsQuery(raw: Record<string, string | string[] | undefined>): PayAnalyticsQuery {
  const out: PayAnalyticsQuery = {};
  const since = one(raw.since);
  if (since && DATE_RE.test(since)) out.since = since;
  const until = one(raw.until);
  if (until && DATE_RE.test(until) && (!out.since || until >= out.since)) out.until = until;
  return out;
}

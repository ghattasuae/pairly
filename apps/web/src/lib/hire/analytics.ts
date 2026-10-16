// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Helpers for the hiring analytics page (M3, brief 3.10): where each record behind a
 * number opens, how a metric's value reads, and the page's query (validated before it
 * reaches the API).
 */
import type { HireMetric, HireRecordLink } from "./types";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** The in-app page of one record: an application on its job's board, or an approval. */
export function recordHref(record: HireRecordLink): string | null {
  if (!UUID_RE.test(record.id)) return null;
  if (record.kind === "approval") return `/inbox/${record.id}`;
  if (record.job_id && UUID_RE.test(record.job_id)) {
    return `/hire/jobs/${record.job_id}/pipeline?candidate=${record.id}`;
  }
  return null;
}

export type ValueParts = { kind: "none" } | { kind: "number"; value: number; unit: string };

/** The value to show, or "none" when there is no data (never a made-up zero). */
export function metricValue(metric: Pick<HireMetric, "value" | "unit">): ValueParts {
  if (metric.value === null || metric.value === undefined || !Number.isFinite(metric.value)) {
    return { kind: "none" };
  }
  return { kind: "number", value: metric.value, unit: metric.unit };
}

export interface AnalyticsQuery {
  job_id?: string;
  since?: string;
  until?: string;
  ghost_days?: number;
  agreement_threshold?: number;
  minutes_per_booking?: number;
}

function one(value: string | string[] | undefined): string | undefined {
  return typeof value === "string" ? value.trim() : undefined;
}

function bounded(value: string | undefined, min: number, max: number): number | undefined {
  if (value === undefined || value === "") return undefined;
  const n = Number(value);
  return Number.isFinite(n) && n >= min && n <= max ? n : undefined;
}

/** The page's filters from the URL; anything invalid is dropped, never sent as is. */
export function analyticsQuery(raw: Record<string, string | string[] | undefined>): AnalyticsQuery {
  const out: AnalyticsQuery = {};
  const job = one(raw.job_id);
  if (job && UUID_RE.test(job)) out.job_id = job;
  const since = one(raw.since);
  if (since && DATE_RE.test(since)) out.since = since;
  const until = one(raw.until);
  if (until && DATE_RE.test(until)) out.until = until;
  const ghost = bounded(one(raw.ghost_days), 1, 365);
  if (ghost !== undefined && Number.isInteger(ghost)) out.ghost_days = ghost;
  const threshold = bounded(one(raw.agreement_threshold), 0, 100);
  if (threshold !== undefined) out.agreement_threshold = threshold;
  const minutes = bounded(one(raw.minutes_per_booking), 0, 240);
  if (minutes !== undefined && Number.isInteger(minutes)) out.minutes_per_booking = minutes;
  return out;
}
